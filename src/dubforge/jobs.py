"""Persistent job state: identity, ownership, heartbeat, lifecycle and attempts.

A job is a small JSON document in `<workdir>/registry/<job-id>.json`, written atomically.
There is no database and no daemon; the requirements — an agent can submit work, ask how
it is going, and get the result — are met by a file per job, a bounded thread pool and two
locks.

Three things in here are worth reading before changing anything.

**Identity is the request, not the file.** A job id is
`<stem>-<pathhash>-<requesthash>`, where the request hash covers everything that changes
the *result*: target language, providers and models, voices, glossary, context, timing and
mix settings, plus a cheap fingerprint of the source. So one video can have a Hindi job and
a Tamil job at the same time, two identical requests deduplicate, and a different provider
configuration can never inherit another run's output. Job ids written before 0.2.1 (two
parts) are still readable.

**Ownership is a claim with a token.** Only the worker that holds the claim may move a job
forward, and the claim is created with `O_EXCL`, so there is no check-then-write race
between processes. A claim from a dead process is reclaimed; a claim from a live process is
never stolen.

**Liveness is a pid *and* a heartbeat.** On the same host, a live pid wins over a stale
heartbeat — a provider call that takes four minutes must not be declared interrupted. Off
that host a pid means nothing, so the heartbeat is the only signal, which is why the worker
thread refreshes it on a timer rather than only between stages.
"""

from __future__ import annotations

import contextlib
import hashlib
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

from . import events
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

# What may follow what. Anything else is a bug in the caller, not a state to tolerate:
# a terminal job that silently becomes active again is how a finished dub gets re-run or,
# worse, a failed one gets reported as done.
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({QUEUED, VALIDATING, PROCESSING, CANCELLED, FAILED, INTERRUPTED}),
    VALIDATING: frozenset({VALIDATING, PROCESSING, FAILED, CANCELLED, INTERRUPTED}),
    PROCESSING: frozenset({PROCESSING, QUALITY_CHECK, COMPLETED, FAILED, CANCELLED, INTERRUPTED}),
    QUALITY_CHECK: frozenset({QUALITY_CHECK, COMPLETED, FAILED, CANCELLED, INTERRUPTED}),
    COMPLETED: frozenset({COMPLETED, QUEUED}),  # QUEUED only via an explicit retry
    FAILED: frozenset({FAILED, QUEUED}),  # QUEUED only via an explicit retry
    CANCELLED: frozenset({CANCELLED, QUEUED}),
    INTERRUPTED: frozenset({INTERRUPTED, QUEUED}),
}

# `<stem>-<pathhash>` (0.2.0) or `<stem>-<pathhash>-<requesthash>` (0.2.1 and later).
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

DEFAULT_HEARTBEAT_STALE_S = 180.0
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0

# The settings that change a dub's *result*. Everything else (workdir, cache, keep_work,
# dry_run, logging, limits) is deliberately excluded: including them would fragment
# deduplication without changing a single frame of output.
REQUEST_FIELDS = (
    "target_lang",
    "source_lang",
    "asr",
    "asr_model",
    "translator",
    "translate_model",
    "tts",
    "tts_model",
    "tts_rate",
    "voice_map",
    "voice_pool",
    "glossary",
    "context",
    "style",
    "emotion",
    "keep_background",
    "bed_gain_db",
    "voice_gain_db",
    "duck",
    "loudness_lufs",
    "true_peak_db",
    "audio_bitrate",
    "max_speed",
    "min_speed",
    "rewrite_rounds",
    "max_segment_s",
    "max_segment_chars",
    "tail_gap_s",
    "speech_cps",
    "transcript",
    "script",
    "fallback",
    # 0.4.0: these change the artifact and were missing from the identity.
    "retranslate",  # keeps supplied translations instead of translating again
    "lipsync",  # re-renders the picture when it is not "none"
    "separator",  # a different stem split produces a different bed
)


class JobError(RuntimeError):
    """A job operation that cannot be completed, with a message a caller can act on."""


def _can_probe_pid() -> bool:
    """Whether a pid means anything on this platform. On Windows it does not."""
    return os.name != "nt"


def _pid_alive(pid: int | None) -> bool:
    """POSIX can probe; Windows cannot, so there the heartbeat decides."""
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


# --------------------------------------------------------------------------- identity


def request_fingerprint(
    settings: Settings, source_fingerprint: str = "", transcript_hash: str = ""
) -> str:
    """A stable hash of everything that changes the result of a dub.

    A supplied transcript is identified by its **content**, not its path: the same file
    edited in place must be a different request, and a transcript copied to a new name must
    be the same one.
    """
    payload = {}
    for name in REQUEST_FIELDS:
        value = getattr(settings, name, None)
        if name == "transcript":
            # The path is not identity: the content is (see `transcript_hash` below), so a
            # copy under another name is the same request and an in-place edit is not.
            value = "supplied" if value else ""
        if isinstance(value, dict):
            value = {str(k): str(v) for k, v in sorted(value.items())}
        elif isinstance(value, list):
            value = [str(item) for item in value]
        payload[name] = value
    payload["source_fingerprint"] = source_fingerprint
    payload["transcript_hash"] = transcript_hash or _transcript_hash(settings)
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _transcript_hash(settings: Settings) -> str:
    """Hash a supplied transcript's bytes. A missing file hashes to a marker, not a crash."""
    path = getattr(settings, "transcript", "") or ""
    if not path:
        return ""
    try:
        return file_content_hash(path)
    except OSError:
        return "missing"


def request_snapshot(settings: Settings) -> dict:
    """The request as a plain dict, so a retry can reproduce it exactly."""
    snapshot = {name: getattr(settings, name, None) for name in REQUEST_FIELDS}
    snapshot["workdir"] = str(settings.workdir)
    return snapshot


_SOURCE_HASHES: dict[str, str] = {}


def source_fingerprint(path: str | Path, use_cache: bool = True) -> str:
    """Identity of the source *content*: streamed SHA-256 plus the size as support.

    This used to be `size:mtime_ns`, which meant two different videos of the same size with
    the same timestamp were the same job — a real collision, not a theoretical one, because
    a copy preserves the timestamp and a re-encode can preserve the size.

    Cost is the reason it was metadata-only: hashing a 2 GB file reads 2 GB. So the hash is
    memoised on `(path, size, mtime_ns)` for the life of the process — a submit hashes once,
    and every later call in the same run (retry, plan, job id) is free. Across processes it
    is one read of the file per job, which is the price of not confusing two videos.

    A rename or a move keeps the same identity, because the path is not part of it: the
    documented policy is that content is the identity.
    """
    target = Path(path)
    try:
        stat = target.stat()
    except OSError as exc:
        raise JobError(f"cannot fingerprint the source {path}: {exc}") from exc
    key = f"{target.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    if use_cache and key in _SOURCE_HASHES:
        return _SOURCE_HASHES[key]

    digest = hashlib.sha256()
    try:
        with open(target, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise JobError(f"cannot read the source {path}: {exc}") from exc
    value = f"{digest.hexdigest()}:{stat.st_size}"
    _SOURCE_HASHES[key] = value
    return value


def file_content_hash(path: str | Path) -> str:
    """SHA-256 of a small file's bytes (a transcript), independent of its path."""
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def new_job_id(source: str | Path, fingerprint: str = "") -> str:
    """`<stem>-<pathhash>` (legacy) or `<stem>-<pathhash>-<requesthash>`."""
    base = f"{slug(Path(source).stem)}-{path_hash(source)}"
    return f"{base}-{fingerprint[:8]}" if fingerprint else base


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
    review: str = ""
    qc_ok: bool | None = None
    qc: dict = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""
    warnings: list[str] = field(default_factory=list)
    provider_retries: int = 0
    cancel_requested: bool = False
    attempts: int = 0
    attempt_history: list[dict] = field(default_factory=list)
    pid: int | None = None
    host: str = field(default_factory=socket.gethostname)
    workdir: str = ""
    fingerprint: str = ""  # the source content fingerprint
    request_fingerprint: str = ""
    request: dict = field(default_factory=dict)
    claim: dict = field(default_factory=dict)
    duplicate: bool = False  # set when a submit found this request already known
    legacy_id: bool = False  # written before 0.2.1: two-part id, no request snapshot

    # ------------------------------------------------------------------ helpers
    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def qc_failed(self) -> bool:
        """Finished, but the dub did not pass QC — not the same thing as a success."""
        return self.status == COMPLETED and self.qc_ok is False

    @property
    def elapsed_s(self) -> float | None:
        if self.duration_s is not None:
            return self.duration_s
        if self.started_at:
            return round(time.time() - self.started_at, 2)
        return None

    def summary(self) -> dict:
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
            "qc_failed": self.qc_failed,
            "failures": self.qc.get("failures", []) if isinstance(self.qc, dict) else [],
            "output": self.output,
            "review": self.review,
            "error": self.error,
            "error_kind": self.error_kind,
            "attempts": self.attempts,
            "provider_retries": self.provider_retries,
            "request_fingerprint": self.request_fingerprint,
            "source_fingerprint": self.fingerprint,
        }

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        known = set(cls.__dataclass_fields__)
        job = cls(**{k: v for k, v in data.items() if k in known})
        job.legacy_id = not job.request_fingerprint
        return job


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
            raise JobError(
                f"invalid job id {job_id!r}: expected letters, digits, dot, dash or underscore"
            )
        return self.registry / f"{job_id}.json"

    def _claim_path(self, job_id: str) -> Path:
        return self.registry / f"{job_id}.claim"

    # ------------------------------------------------------------------- write
    def save(self, job: Job) -> Job:
        job.updated_at = time.time()
        atomic_write_text(
            self._path(job.id), json.dumps(job.to_dict(), ensure_ascii=False, indent=2)
        )
        return job

    def create(self, source: str | Path, settings: Settings, **extra) -> Job:
        """Create a job for this source *and request*, or return the one already known."""
        source = str(Path(source).expanduser().resolve())
        source_hash = extra.pop("source_fingerprint", None) or source_fingerprint(source)
        fingerprint = extra.pop("request_fingerprint", None) or request_fingerprint(
            settings, source_hash
        )
        job_id = new_job_id(source, fingerprint)
        with self._lock:
            existing = self.get(job_id)
        if existing is not None:
            existing.duplicate = True
            return existing
        events.emit(
            events.JOB_CREATED,
            job=job_id,
            target_lang=settings.target_lang,
            source=source,
            request_fingerprint=fingerprint,
        )
        job = Job(
            id=job_id,
            source=source,
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
            fingerprint=source_hash,
            request_fingerprint=fingerprint,
            request=request_snapshot(settings),
            **extra,
        )
        return self.save(job)

    def transition(self, job: Job, status: str, **fields) -> Job:
        """Move a job to `status`, refusing an illegal move."""
        if status != job.status and status not in TRANSITIONS.get(job.status, frozenset()):
            raise JobError(
                f"illegal transition for {job.id}: {job.status} → {status} "
                f"(allowed: {', '.join(sorted(TRANSITIONS.get(job.status, ()))) or 'none'})"
            )
        if status == COMPLETED and fields.get("qc_ok") is None:
            # A completion without a verdict is a bug, not a state: record it as "did not
            # pass" and say why, so a job is never both finished and undecided.
            fields["qc_ok"] = False
            fields.setdefault("warnings", [])
            if isinstance(fields["warnings"], list):
                fields["warnings"] = [*fields["warnings"], "completed without a QC verdict"]
        job.status = status
        for key, value in fields.items():
            if key in Job.__dataclass_fields__:
                setattr(job, key, value)
        return self.save(job)

    def update(self, job_id: str, token: str | None = None, **fields) -> Job:
        """Update fields, validating any status change and honouring ownership."""
        with self._lock:
            job = self.get(job_id, recover=False)
            if job is None:
                raise JobError(f"no job {job_id!r}")
            if token is not None and job.claim and job.claim.get("token") != token:
                raise JobError(
                    f"job {job_id} is owned by another worker "
                    f"({job.claim.get('host')}:{job.claim.get('pid')})"
                )
            status = fields.pop("status", None)
            if status is not None:
                job = self.transition(job, status)
            for key, value in fields.items():
                if key in Job.__dataclass_fields__:
                    setattr(job, key, value)
            # Touching a job with a live pid *is* a liveness signal. Without this, a record
            # that carries a pid but no heartbeat (every 0.2.0 job, and any caller that sets
            # only `pid`) is indistinguishable from a crashed one where pids cannot be
            # probed — which is exactly how a running job gets declared interrupted.
            if job.pid and not job.heartbeat_at:
                job.heartbeat_at = time.time()
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
        """Reconcile one job with reality. A live job is never taken for a dead one."""
        if not job.active:
            return job

        # A job nobody has claimed yet is waiting, not dead. This matters because
        # `get()` reconciles on every read, and an unclaimed job is read constantly.
        if not job.claim and not job.pid and not job.heartbeat_at:
            return job

        claim = job.claim or {}
        claim_host = claim.get("host") or job.host
        same_host = claim_host == socket.gethostname()
        pid = claim.get("pid") or job.pid
        heartbeat = job.heartbeat_at or claim.get("since")
        stale_after = float(
            os.environ.get("DUBFORGE_JOB_HEARTBEAT_STALE_S", DEFAULT_HEARTBEAT_STALE_S)
        )
        now = time.time()
        fresh = heartbeat is not None and (now - heartbeat) <= stale_after
        if heartbeat is not None and heartbeat > now:
            fresh = True  # clock skew between hosts must not look like death

        # Same host and a probeable pid: the pid is the truth. A provider call that takes
        # four minutes must not be declared interrupted because the heartbeat is old — the
        # process is demonstrably alive.
        if same_host and pid and _can_probe_pid():
            if _pid_alive(pid):
                return job
            return self._mark_interrupted(job, "the process running this job exited")
        # Otherwise (another host, or Windows where probing a pid is unsafe) the heartbeat
        # is the only evidence there is.
        if fresh:
            return job
        return self._mark_interrupted(job, "no heartbeat from the worker running this job")

    def _mark_interrupted(self, job: Job, reason: str) -> Job:
        events.emit(events.JOB_RECOVERED, job=job.id, reason=reason, status=INTERRUPTED)
        job.status = INTERRUPTED
        job.stage = job.stage or PROCESSING
        job.error = reason
        job.error_kind = "interrupted"
        job.finished_at = job.finished_at or time.time()
        if job.started_at:
            job.duration_s = round(job.finished_at - job.started_at, 2)
        job.pid = None
        job.claim = {}
        _close_attempt(job, INTERRUPTED, error=reason)
        unlink_retry(self._claim_path(job.id))
        # Recovery happens on read, and reads are concurrent: several threads can reach the
        # same conclusion at once. Serialise the write and treat a lost race as "already
        # recorded" — the caller still gets the interrupted job it asked about.
        with self._lock, contextlib.suppress(OSError):
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

    def output_paths(self) -> set[str]:
        """Every output any job has produced — read without recovery, so it scales.

        A batch uses this to avoid dubbing its own results. It deliberately does not build
        `Job` objects or reconcile state: with thousands of records that would cost seconds
        for a set of strings.
        """
        outputs: set[str] = set()
        for path in self.registry.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            output = data.get("output") if isinstance(data, dict) else None
            if output:
                with contextlib.suppress(OSError, ValueError):
                    outputs.add(str(Path(output).resolve()))
        return outputs

    def find_by_request(self, source: str | Path, settings: Settings) -> Job | None:
        """The job for this exact request, if one exists."""
        fingerprint = request_fingerprint(settings, source_fingerprint(source))
        return self.get(new_job_id(str(Path(source).expanduser().resolve()), fingerprint))

    # ------------------------------------------------------------------ claims
    def claim(
        self, job_id: str, token: str, pid: int | None = None, host: str | None = None
    ) -> bool:
        """Atomically take ownership. One winner, no check-then-write race."""
        claim_path = self._claim_path(job_id)
        payload = {
            "token": token,
            "pid": pid or os.getpid(),
            "host": host or socket.gethostname(),
            "since": time.time(),
        }
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = self._read_claim(job_id)
            if existing and self._claim_is_live(existing):
                return False
            unlink_retry(claim_path)
            try:
                fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False  # lost the race to another process: that is a correct outcome
        except OSError as exc:
            raise JobError(f"cannot create a claim for {job_id}: {exc}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        events.emit(events.JOB_CLAIMED, job=job_id, pid=payload["pid"], host=payload["host"])
        with self._lock:
            job = self.get(job_id, recover=False)
            if job is None:
                unlink_retry(claim_path)
                raise JobError(f"no job {job_id!r}")
            job.claim = payload
            job.pid = payload["pid"]
            job.heartbeat_at = time.time()
            self.save(job)
        return True

    def _read_claim(self, job_id: str) -> dict:
        try:
            return json.loads(self._claim_path(job_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _claim_is_live(self, claim: dict) -> bool:
        host = claim.get("host") or ""
        pid = claim.get("pid")
        since = claim.get("since") or 0
        stale_after = float(
            os.environ.get("DUBFORGE_JOB_HEARTBEAT_STALE_S", DEFAULT_HEARTBEAT_STALE_S)
        )
        if host == socket.gethostname() and pid and _can_probe_pid():
            return _pid_alive(pid)
        return (time.time() - since) <= stale_after

    def owns(self, job_id: str, token: str | None) -> bool:
        if token is None:
            return False
        return (self._read_claim(job_id) or {}).get("token") == token

    def heartbeat(self, job_id: str, token: str) -> bool:
        """Refresh liveness for the owner. A stale worker must not touch a newer attempt."""
        if not self.owns(job_id, token):
            return False
        with self._lock:
            job = self.get(job_id, recover=False)
            if job is None or job.terminal:
                return False
            job.heartbeat_at = time.time()
            self.save(job)
        return True

    def release(self, job_id: str, token: str) -> None:
        if not self.owns(job_id, token):
            return
        unlink_retry(self._claim_path(job_id))
        with self._lock:
            job = self.get(job_id, recover=False)
            if job is not None:
                job.claim = {}
                self.save(job)

    # ----------------------------------------------------------------- control
    def request_cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}")
        if job.terminal:
            return job
        job.cancel_requested = True
        if job.status in (QUEUED, VALIDATING):
            job = self.transition(job, CANCELLED, stage=CANCELLED, finished_at=time.time())
            _close_attempt(job, CANCELLED, error="cancelled before it started")
            self.save(job)
        else:
            self.save(job)
        return job

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get(job_id, recover=False)
        return bool(job and (job.cancel_requested or job.status == CANCELLED))

    def retry(self, job_id: str, max_retries: int = 2) -> Job:
        """Re-queue a finished job as a *new attempt*, preserving the original request."""
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
        job.claim = {}
        job.heartbeat_at = None
        # A new attempt must not present the previous attempt's files as its result.
        job.output = ""
        job.audio = ""
        job.transcript_json = ""
        job.subtitles = {}
        job.report = ""
        job.review = ""
        job.warnings = []
        job.providers_used = {}
        job.attempts += 1
        events.emit(events.JOB_RETRY, job=job_id, attempt=job.attempts)
        job.attempt_history = (
            list(job.attempt_history or [])
            + [{"attempt": job.attempts, "event": "retry_requested", "at": time.time()}]
        )[-50:]  # bounded, like every other write to this list
        return self.save(job)

    def settings_for(self, job_id: str) -> Settings:
        """Rebuild the settings a job was submitted with, so a retry is identical."""
        job = self.get(job_id)
        if job is None:
            raise JobError(f"no job {job_id!r}")
        if not job.request:
            raise JobError(
                f"job {job_id} was created before request snapshots existed; "
                "re-submit it instead of retrying"
            )
        return Settings().with_overrides(**{k: v for k, v in job.request.items() if v is not None})

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
            removed.append(job.id)
            if dry_run:
                continue
            unlink_retry(path)
            unlink_retry(self._claim_path(job.id))
            _remove_job_dir(self.workdir, job)
        events.emit(events.CLEANUP, removed=len(removed), kept=kept, dry_run=dry_run)
        return {"removed": removed, "kept": kept, "dry_run": dry_run}


def _close_attempt(job: Job, status: str, **extra) -> None:
    history = list(job.attempt_history or [])
    if history and history[-1].get("attempt") == job.attempts and "finished_at" not in history[-1]:
        history[-1].update({"status": status, "finished_at": time.time(), **extra})
    elif job.attempts:
        history.append({"attempt": job.attempts, "status": status, "at": time.time(), **extra})
    job.attempt_history = history[-50:]  # bounded: history is for diagnosis, not an archive


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
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- runner


class JobRunner:
    """Submit a dub, get a job id back, ask how it is going later.

    A bounded thread pool and a claim file, not a queue server. `max_concurrent_jobs`
    bounds the pool so an agent cannot start ten dubs by calling a tool ten times, and the
    claim means two `JobRunner` instances — in one process or several — cannot both run the
    same request.
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
        self._heartbeats: dict[str, threading.Event] = {}
        self._closing = False

    # ------------------------------------------------------------------ submit
    def submit(
        self,
        source: str | Path,
        settings: Settings | None = None,
        output: str | Path | None = None,
    ) -> Job:
        """Queue a dub. Returns immediately; the same request never runs twice at once."""
        settings = settings or self.settings
        job = self.store.create(source, settings, output=str(output) if output else "")
        with self._lock:
            if job.id in self._inflight:
                job.duplicate = True
                return job
            if job.active and (self.store._read_claim(job.id) or {}).get("token"):
                # Another worker (possibly another process) owns it: report, do not queue.
                job.duplicate = True
                return job
            self._inflight.add(job.id)

        if job.terminal:
            if job.status == COMPLETED:
                job = self.store.update(
                    job.id,
                    status=QUEUED,
                    stage=QUEUED,
                    progress=0.0,
                    qc_ok=None,
                    qc={},
                    error="",
                    error_kind="",
                )
            else:
                with contextlib.suppress(JobError):
                    job = self.store.retry(job.id, max_retries=max(1, self.settings.max_retries))
            job = self.store.update(job.id, status=QUEUED, stage=QUEUED)
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

        token = new_token()
        if not self.store.claim(job_id, token):
            # Another worker owns this request. Nothing to do, and nothing to report:
            # the caller already has the job document.
            with self._lock:
                self._inflight.discard(job_id)
                self._futures.pop(job_id, None)
            return

        stop = threading.Event()
        self._start_heartbeat(job_id, token, stop)
        started = time.time()
        attempt = 0
        try:
            job = self.store.get(job_id, recover=False)
            attempt = max(1, job.attempts if job else 1)
            history = list(job.attempt_history if job else [])
            history.append(
                {
                    "attempt": attempt,
                    "event": "started",
                    "at": started,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                }
            )
            self.store.update(
                job_id,
                token=token,
                status=VALIDATING,
                stage=VALIDATING,
                started_at=started,
                heartbeat_at=started,
                pid=os.getpid(),
                attempts=attempt,
                attempt_history=history[-50:],
                output=output,
                # A new attempt starts clean: the previous attempt's files are not its result.
                qc={},
                qc_ok=None,
                error="",
                error_kind="",
                warnings=[],
                providers_used={},
            )

            plan = pipeline.plan(self.store.get(job_id, recover=False).source, settings)
            self.store.update(
                job_id,
                token=token,
                status=PROCESSING,
                stage=PROCESSING,
                providers_used=plan.providers,
                heartbeat_at=time.time(),
            )

            def progress(stage: str, done: int, total: int) -> None:
                events.emit(events.JOB_STAGE, job=job_id, stage=stage, done=done, total=total)
                self.store.update(
                    job_id,
                    token=token,
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
                job_id,
                token=token,
                status=QUALITY_CHECK,
                stage=QUALITY_CHECK,
                heartbeat_at=time.time(),
            )
            job = self.store.get(job_id, recover=False)
            _close_attempt(job, COMPLETED, qc_ok=result.ok)
            events.emit(
                events.JOB_COMPLETED,
                job=job_id,
                qc_ok=result.ok,
                duration_s=round(time.time() - started, 2),
                retries=len(result.retries),
                failures=result.qc.get("failures", []),
            )
            events.emit(
                events.QC_RESULT,
                job=job_id,
                ok=result.ok,
                failures=result.qc.get("failures", []),
                warnings=result.qc.get("warnings", []),
            )
            self.store.update(
                job_id,
                token=token,
                status=COMPLETED,
                stage=COMPLETED,
                progress=1.0,
                output=result.output,
                audio=result.audio,
                transcript_json=result.transcript_json,
                subtitles=result.subtitles,
                report=result.report,
                review=result.review,
                qc_ok=bool(result.ok),
                qc={
                    "ok": result.ok,
                    "failures": result.qc.get("failures", []),
                    "warnings": result.qc.get("warnings", []),
                    "metrics": result.qc.get("metrics", {}),
                },
                providers_used=result.providers_used,
                warnings=result.warnings,
                provider_retries=len(result.retries),
                finished_at=time.time(),
                duration_s=round(time.time() - started, 2),
                heartbeat_at=time.time(),
                pid=None,
                attempt_history=job.attempt_history,
            )
        except pipeline.DubCancelled as exc:
            events.emit(events.JOB_CANCELLED, job=job_id, reason=str(exc)[:200])
            self._finish(job_id, token, CANCELLED, str(exc), "cancelled", started)
        except Exception as exc:  # noqa: BLE001 - a worker must never take the server down
            kind = exc.kind if isinstance(exc, ProviderError) else type(exc).__name__
            events.emit(events.JOB_FAILED, job=job_id, error_kind=kind, error=str(exc)[:200])
            self._finish(job_id, token, FAILED, str(exc)[:800], kind, started)
        finally:
            stop.set()
            self._join_heartbeat(job_id)
            self.store.release(job_id, token)
            with self._lock:
                self._inflight.discard(job_id)
                self._futures.pop(job_id, None)

    def _finish(
        self, job_id: str, token: str, status: str, error: str, kind: str, started: float
    ) -> None:
        job = self.store.get(job_id, recover=False)
        if job is not None:
            _close_attempt(job, status, error=error, error_kind=kind)
        with contextlib.suppress(JobError):
            self.store.update(
                job_id,
                token=token,
                status=status,
                stage=status,
                error=error,
                error_kind=kind,
                finished_at=time.time(),
                duration_s=round(time.time() - started, 2),
                pid=None,
                attempt_history=job.attempt_history if job else [],
            )

    # -------------------------------------------------------------- heartbeat
    def _start_heartbeat(self, job_id: str, token: str, stop: threading.Event) -> None:
        interval = max(
            1.0,
            float(getattr(self.settings, "job_heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S)),
        )

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    if not self.store.heartbeat(job_id, token):
                        return  # lost ownership or the job finished: stop beating
                except Exception:  # noqa: BLE001 - a heartbeat must never kill a dub
                    return

        thread = threading.Thread(target=beat, name=f"dubforge-hb-{job_id}", daemon=True)
        thread.start()
        with self._lock:
            self._heartbeats[job_id] = stop

    def _join_heartbeat(self, job_id: str) -> None:
        with self._lock:
            self._heartbeats.pop(job_id, None)

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
            "attempt_history": job.attempt_history,
        }

    # ---------------------------------------------------------------- control
    def cancel(self, job_id: str) -> Job:
        return self.store.request_cancel(job_id)

    def retry(self, job_id: str) -> Job:
        """Retry with the job's *original* request, not the current settings."""
        settings = self.store.settings_for(job_id)
        self.store.retry(job_id, max_retries=self.settings.max_retries)
        job = self.store.get(job_id, recover=False)
        return self.submit(job.source, settings, job.output or None)

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
        """Stop accepting work, stop the heartbeats, mark anything running as interrupted."""
        self._closing = True
        with self._lock:
            running = set(self._inflight)
            stops = list(self._heartbeats.values())
        for stop in stops:
            stop.set()
        for job_id in running:
            job = self.store.get(job_id, recover=False)
            if job is not None and job.active:
                with contextlib.suppress(JobError):
                    self.store.update(
                        job_id,
                        status=INTERRUPTED,
                        error="the application shut down while this job was running",
                        error_kind="shutdown",
                        finished_at=time.time(),
                        pid=None,
                    )
        self._executor.shutdown(wait=wait, cancel_futures=True)
