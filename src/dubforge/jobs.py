"""Persistent job state: what was submitted, where it got to, and what came out.

A job is a small JSON document in `<workdir>/registry/<job-id>.json`, written
atomically. There is no database and no daemon, because the requirement — "an agent can
submit work, ask how it is going, and get the result" — is satisfied by a file per job
and a thread pool. Adding SQLite here would buy nothing and cost a dependency.

The job id is derived from the source *path* (`<stem>-<pathhash>`), which is the same
identity the workspace uses. That gives two properties for free:

* submitting the same file twice returns the same job instead of starting a second dub;
* the job's artefacts live in the workspace directory the pipeline already uses, so the
  manifest and the job document cannot drift into different stories.

`interrupted` is a real state, not a failure: a process that dies mid-run leaves a job
in `processing` with a dead pid. On the next read that becomes `interrupted`, and it is
never reported as `completed` — the one lie a job store must never tell.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .workspace import atomic_write_text, path_hash, slug, unlink_retry

QUEUED = "queued"
VALIDATING = "validating"
PROCESSING = "processing"
QUALITY_CHECK = "quality_check"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

ACTIVE = (QUEUED, VALIDATING, PROCESSING, QUALITY_CHECK)
TERMINAL = (COMPLETED, FAILED, CANCELLED, INTERRUPTED)
STATUSES = (*ACTIVE, *TERMINAL)

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HEARTBEAT_STALE_S = 120.0


class JobError(RuntimeError):
    """A job operation that cannot be completed, with a message a caller can act on."""


def new_job_id(source: str | Path) -> str:
    return f"{slug(Path(source).stem)}-{path_hash(source)}"


def _pid_alive(pid: int | None) -> bool:
    """POSIX can probe; Windows cannot, so there we trust the heartbeat instead."""
    if not pid:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


@dataclass
class Job:
    id: str
    source: str
    target_lang: str
    source_lang: str = "auto"
    status: str = QUEUED
    stage: str = QUEUED
    progress: float = 0.0
    steps_done: int = 0
    steps_total: int = 11
    providers: dict[str, str] = field(default_factory=dict)  # what was requested
    providers_used: dict[str, str] = field(default_factory=dict)  # what actually ran
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    heartbeat_at: float | None = None
    output: str = ""
    audio: str = ""
    transcript_json: str = ""
    subtitles: dict = field(default_factory=dict)
    report: str = ""
    qc_ok: bool | None = None
    qc: dict = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""
    warnings: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    attempts: int = 0
    pid: int | None = None
    host: str = field(default_factory=socket.gethostname)
    workdir: str = ""
    fingerprint: str = ""
    duplicate: bool = False  # set when a submit found this job already running

    # ------------------------------------------------------------------ helpers
    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def elapsed_s(self) -> float | None:
        if self.duration_s is not None:
            return self.duration_s
        if self.started_at:
            return round(time.time() - self.started_at, 2)
        return None

    def summary(self) -> dict:
        """A compact view for listings — no paths a caller cannot use, no transcript."""
        return {
            "job_id": self.id,
            "source": self.source,
            "target_lang": self.target_lang,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "steps": f"{self.steps_done}/{self.steps_total}",
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "duration_s": self.elapsed_s,
            "qc_ok": self.qc_ok,
            "failures": self.qc.get("failures", []) if isinstance(self.qc, dict) else [],
            "output": self.output,
            "error": self.error,
            "error_kind": self.error_kind,
            "attempts": self.attempts,
        }

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --------------------------------------------------------------------------- store


class JobStore:
    """One JSON document per job, atomically written, no database."""

    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.registry = self.workdir / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------- paths
    def _path(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id or ""):
            # A job id is used as a filename. Validate before touching the filesystem.
            raise JobError(
                f"invalid job id {job_id!r}: expected letters, digits, dot, dash or underscore"
            )
        return self.registry / f"{job_id}.json"

    # ------------------------------------------------------------------- write
    def save(self, job: Job) -> Job:
        job.updated_at = time.time()
        atomic_write_text(
            self._path(job.id), json.dumps(job.to_dict(), ensure_ascii=False, indent=2)
        )
        return job

    def create(self, source: str | Path, settings: Settings, **extra) -> Job:
        """Create a job, or return the existing one if this source is already known.

        Returning the existing job is what stops an agent that retries a tool call from
        queueing the same film twice.
        """
        job_id = new_job_id(source)
        with self._lock:
            existing = self.get(job_id)
        if existing is not None:
            existing.duplicate = True
            return existing
        job = Job(
            id=job_id,
            source=str(Path(source).expanduser().resolve()),
            target_lang=settings.target_lang,
            source_lang=settings.source_lang,
            providers={
                "asr": settings.asr,
                "translate": settings.translator,
                "tts": settings.tts,
                "separator": settings.separator,
                "lipsync": settings.lipsync,
            },
            workdir=str(Path(settings.workdir).resolve()),
            **extra,
        )
        return self.save(job)

    def update(self, job_id: str, **fields) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise JobError(f"no job {job_id!r}")
            for key, value in fields.items():
                if key in Job.__dataclass_fields__:
                    setattr(job, key, value)
            return self.save(job)

    # -------------------------------------------------------------------- read
    def get(self, job_id: str, recover: bool = True) -> Job | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        try:
            job = Job.from_dict(data)
        except TypeError:
            return None
        if recover:
            job = self._recover_one(job)
        return job

    def _recover_one(self, job: Job) -> Job:
        """A job left active by a dead process is interrupted, never completed."""
        if not job.active:
            return job
        same_host = job.host == socket.gethostname()
        heartbeat_old = (
            job.heartbeat_at is not None and (time.time() - job.heartbeat_at) > HEARTBEAT_STALE_S
        )
        # A job nobody has claimed yet (no pid) is queued, not dead. A job that *has* a
        # pid which no longer exists is the debris of a crashed process.
        if job.pid is None and not heartbeat_old:
            return job
        if (same_host and job.pid is not None and not _pid_alive(job.pid)) or heartbeat_old:
            job.status = INTERRUPTED
            job.stage = job.stage or PROCESSING
            job.error = "the process running this job stopped responding"
            job.error_kind = "interrupted"
            job.finished_at = job.finished_at or time.time()
            if job.started_at:
                job.duration_s = round(job.finished_at - job.started_at, 2)
            job.pid = None
            self.save(job)
        return job

    def list(
        self,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
        target_lang: str | None = None,
        source: str | None = None,
    ) -> tuple[list[Job], int]:
        """Newest first, with a total so a caller can paginate honestly."""
        jobs: list[Job] = []
        for path in self.registry.glob("*.json"):
            job = self.get(path.stem)
            if job is None:
                continue
            if status and job.status != status:
                continue
            if target_lang and job.target_lang != target_lang:
                continue
            if source and Path(source).resolve() != Path(job.source):
                continue
            jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        total = len(jobs)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        return jobs[offset : offset + limit], total

    # ----------------------------------------------------------------- control
    def request_cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}")
        if job.terminal:
            return job
        job.cancel_requested = True
        # A queued job can be cancelled immediately; a running one is asked politely.
        if job.status in (QUEUED, VALIDATING):
            job.status = CANCELLED
            job.stage = CANCELLED
            job.finished_at = time.time()
        return self.save(job)

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get(job_id, recover=False)
        return bool(job and (job.cancel_requested or job.status == CANCELLED))

    def retry(self, job_id: str, max_retries: int = 2) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}")
        if job.status not in (FAILED, INTERRUPTED, CANCELLED):
            raise JobError(
                f"job {job_id} is {job.status}; only failed, interrupted or cancelled jobs can be retried"
            )
        if job.attempts >= max_retries + 1:
            raise JobError(f"job {job_id} has used its {max_retries + 1} attempts")
        job.status = QUEUED
        job.stage = QUEUED
        job.progress = 0.0
        job.steps_done = 0
        job.cancel_requested = False
        job.error = ""
        job.error_kind = ""
        job.qc = {}
        job.qc_ok = None
        job.started_at = None
        job.finished_at = None
        job.duration_s = None
        job.pid = None
        job.attempts += 1
        return self.save(job)

    # ---------------------------------------------------------------- recovery
    def recover(self) -> list[Job]:
        """Reconcile every job on disk with reality. Called on startup and on demand."""
        recovered: list[Job] = []
        for path in self.registry.glob("*.json"):
            job = self.get(path.stem)
            if job is not None and job.status == INTERRUPTED:
                recovered.append(job)
        return recovered

    # ----------------------------------------------------------------- cleanup
    def cleanup(
        self,
        older_than_days: int | None = None,
        statuses: tuple[str, ...] = TERMINAL,
        dry_run: bool = False,
    ) -> dict:
        """Delete finished job documents (and their job directories) inside the workdir."""
        cutoff = time.time() - (older_than_days or 0) * 86400 if older_than_days else None
        removed: list[str] = []
        kept = 0
        for path in list(self.registry.glob("*.json")):
            job = self.get(path.stem)
            if job is None:
                continue
            if job.status not in statuses:
                kept += 1
                continue
            if cutoff and (job.finished_at or job.updated_at) > cutoff:
                kept += 1
                continue
            if dry_run:
                removed.append(job.id)
                continue
            if not dry_run:
                unlink_retry(path)
                _remove_job_dir(self.workdir, job)
            removed.append(job.id)
        return {"removed": removed, "kept": kept, "dry_run": dry_run}


def _remove_job_dir(workdir: Path, job: Job) -> None:
    """Delete the job's workspace directory — and nothing outside the workdir."""
    import shutil

    jobs_root = (Path(workdir) / "jobs").resolve()
    candidate = (jobs_root / job.id).resolve()
    try:
        candidate.relative_to(jobs_root)
    except ValueError:
        return  # not under this workdir: refuse to delete
    if candidate.is_dir():
        shutil.rmtree(candidate, ignore_errors=True)


def store_for(settings: Settings) -> JobStore:
    return JobStore(settings.workdir)


def new_token() -> str:
    """Short random token, for callers that need to tag a submission."""
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- runner


class JobRunner:
    """Submit a dub, get a job id back, ask how it is going later.

    A thread pool, not a queue server: the work is I/O- and ffmpeg-bound, the state is a
    JSON file, and a restart is recovered from disk. `max_concurrent_jobs` bounds the
    pool, so an agent cannot start ten dubs by calling a tool ten times.

    Two honest limits, documented rather than hidden:

    * cancellation is cooperative — it is checked between stages and before every paid
      call, so a line in flight finishes and an ffmpeg run in progress is not killed;
    * a worker that dies with the process leaves a job in `processing`, which `recover()`
      turns into `interrupted` on the next read. It is never reported as completed.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.store = JobStore(self.settings.workdir)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.settings.max_concurrent_jobs),
            thread_name_prefix="dubforge-job",
        )
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._futures: dict[str, object] = {}
        self._closing = False

    # ------------------------------------------------------------------ submit
    def submit(
        self,
        source: str | Path,
        settings: Settings | None = None,
        output: str | Path | None = None,
    ) -> Job:
        """Queue a dub. Returns immediately; the same source never runs twice at once."""
        settings = settings or self.settings
        job = self.store.create(source, settings, output=str(output) if output else "")
        with self._lock:
            if job.id in self._inflight:
                job.duplicate = True
                return job
            self._inflight.add(job.id)
        if job.terminal:
            # Re-submitting a finished job is a new attempt, not a no-op.
            job = (
                self.store.retry(job.id, max_retries=self.settings.max_retries)
                if job.status != COMPLETED
                else job
            )
            if job.status == COMPLETED:
                job = self.store.update(
                    job.id,
                    status=QUEUED,
                    stage=QUEUED,
                    attempts=job.attempts + 1,
                    qc_ok=None,
                    qc={},
                    error="",
                    error_kind="",
                    progress=0.0,
                )
        else:
            job = self.store.update(job.id, status=QUEUED, stage=QUEUED)
        self._futures[job.id] = self._executor.submit(
            self._work, job.id, settings, str(output) if output else ""
        )
        return job

    # ------------------------------------------------------------------- work
    def _work(self, job_id: str, settings: Settings, output: str) -> None:
        from . import pipeline
        from .providers.base import ProviderError

        started = time.time()
        self.store.update(
            job_id,
            status=VALIDATING,
            stage=VALIDATING,
            started_at=started,
            heartbeat_at=started,
            pid=os.getpid(),
            output=output,
        )
        try:
            plan = pipeline.plan(self.store.get(job_id, recover=False).source, settings)
            self.store.update(
                job_id,
                status=PROCESSING,
                stage=PROCESSING,
                providers_used=plan.providers,
                heartbeat_at=time.time(),
            )

            def progress(stage: str, done: int, total: int) -> None:
                self.store.update(
                    job_id,
                    stage=stage,
                    steps_done=done,
                    steps_total=total,
                    progress=round(min(1.0, done / max(1, total)), 3),
                    heartbeat_at=time.time(),
                )

            result = pipeline.dub(
                plan.source,
                settings,
                output=output or None,
                cancel=lambda: self.store.is_cancelled(job_id),
                progress=progress,
            )
            self.store.update(
                job_id, status=QUALITY_CHECK, stage=QUALITY_CHECK, heartbeat_at=time.time()
            )
            self.store.update(
                job_id,
                status=COMPLETED,
                stage=COMPLETED,
                progress=1.0,
                output=result.output,
                audio=result.audio,
                transcript_json=result.transcript_json,
                subtitles=result.subtitles,
                report=result.report,
                qc_ok=result.ok,
                qc={
                    "ok": result.ok,
                    "failures": result.qc.get("failures", []),
                    "warnings": result.qc.get("warnings", []),
                    "metrics": result.qc.get("metrics", {}),
                },
                providers_used=result.providers_used,
                warnings=result.warnings,
                finished_at=time.time(),
                duration_s=round(time.time() - started, 2),
                heartbeat_at=time.time(),
                pid=None,
            )
        except pipeline.DubCancelled as exc:
            self.store.update(
                job_id,
                status=CANCELLED,
                stage=CANCELLED,
                error=str(exc),
                error_kind="cancelled",
                finished_at=time.time(),
                duration_s=round(time.time() - started, 2),
                pid=None,
            )
        except Exception as exc:  # noqa: BLE001 - a worker must never take the server down
            kind = exc.kind if isinstance(exc, ProviderError) else type(exc).__name__
            self.store.update(
                job_id,
                status=FAILED,
                stage=FAILED,
                error=str(exc)[:800],
                error_kind=kind,
                finished_at=time.time(),
                duration_s=round(time.time() - started, 2),
                pid=None,
            )
        finally:
            with self._lock:
                self._inflight.discard(job_id)
            self._futures.pop(job_id, None)

    # ------------------------------------------------------------------- read
    def status(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}")
        return job

    def result(self, job_id: str) -> dict:
        job = self.status(job_id)
        return {
            "job": job.summary(),
            "output": job.output,
            "audio": job.audio,
            "transcript_json": job.transcript_json,
            "subtitles": job.subtitles,
            "report": job.report,
            "qc": job.qc,
            "providers_used": job.providers_used,
            "warnings": job.warnings,
            "error": job.error,
        }

    # ---------------------------------------------------------------- control
    def cancel(self, job_id: str) -> Job:
        return self.store.request_cancel(job_id)

    def retry(self, job_id: str) -> Job:
        job = self.store.retry(job_id, max_retries=self.settings.max_retries)
        return self.submit(job.source, self.settings, job.output or None)

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        """Block until the job reaches a terminal state (for the CLI and for tests)."""
        deadline = time.time() + (timeout if timeout is not None else 24 * 3600)
        while time.time() < deadline:
            job = self.status(job_id)
            if job.terminal:
                return job
            time.sleep(0.25)
        raise JobError(f"job {job_id} did not finish within {timeout}s")

    def shutdown(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and mark anything still running as interrupted."""
        self._closing = True
        with self._lock:
            running = set(self._inflight)
        for job_id in running:
            job = self.store.get(job_id, recover=False)
            if job is not None and job.active:
                self.store.update(
                    job_id,
                    status=INTERRUPTED,
                    error="the application shut down while this job was running",
                    error_kind="shutdown",
                    finished_at=time.time(),
                    pid=None,
                )
        self._executor.shutdown(wait=wait, cancel_futures=True)
