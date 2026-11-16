"""One job, one directory, and nothing else touches it.

The old layout put every job in ``<workdir>/<file stem>``, which quietly assumed two
things that are not true: that a filename identifies a file, and that only one dub is
running at a time. Two lessons both called ``lesson.mp4`` from different folders shared
a work directory and overwrote each other's audio; two dubs started at once did the same
thing to themselves.

This module gives every job a directory derived from the *path* (so same-named files
never collide), takes an exclusive lock on it (so concurrent jobs get their own), reuses
it when the previous run was interrupted, and refuses to delete anything that is not
inside it.

Layout::

    <workdir>/
      jobs/<stem>-<pathhash>/          one directory per source path
        .lock                          pid + start time, for concurrency
        manifest.json                  what this job is, and how far it got
        …artefacts
      cache/<content-key>/transcript.json
                                       shared across jobs, keyed by file *content*
                                       and ASR settings, written atomically

The cache is deliberately outside the job directory: the same video rendered into two
languages should pay for recognition once, and two copies of the same file should share
a transcription only when their bytes really match.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

STALE_AFTER_S = 24 * 3600  # a lock older than this is from a dead run, not a live one
MAX_JOB_SUFFIXES = 32
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceError(RuntimeError):
    """The job directory cannot be prepared or safely cleaned."""


def slug(name: str, limit: int = 48) -> str:
    """A filesystem-safe stem. Keeps it readable; the hash carries the uniqueness."""
    cleaned = _SAFE.sub("-", name).strip(".-") or "job"
    return cleaned[:limit]


def path_hash(path: str | Path) -> str:
    """8 hex characters identifying the source *path*, not its content."""
    absolute = str(Path(path).expanduser().resolve())
    return hashlib.sha256(absolute.encode()).hexdigest()[:8]


def _pid_alive(pid: int) -> bool:
    """True if `pid` looks alive.

    Deliberately POSIX-only: on Windows ``os.kill(pid, 0)`` does not probe, it signals,
    so liveness there is judged by the lock's age alone.
    """
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


@dataclass
class JobWorkspace:
    """A locked, unique directory for one dub, plus its shared cache location."""

    path: Path
    cache_dir: Path
    source: str
    fingerprint: str = ""
    reused: bool = False
    notes: list[str] = field(default_factory=list)
    _lock: Path | None = None

    # ------------------------------------------------------------------ files
    def file(self, name: str) -> Path:
        """A path inside the job directory. Refuses to escape it."""
        target = (self.path / name).resolve()
        if not self.contains(target):
            raise WorkspaceError(f"refusing to write outside the job directory: {name!r}")
        return target

    def contains(self, candidate: str | Path) -> bool:
        try:
            Path(candidate).resolve().relative_to(self.path.resolve())
            return True
        except (ValueError, OSError):
            return False

    def dir(self, name: str) -> Path:
        target = self.file(name)
        target.mkdir(parents=True, exist_ok=True)
        return target

    # --------------------------------------------------------------- manifest
    def manifest_path(self) -> Path:
        return self.file("manifest.json")

    def read_manifest(self) -> dict:
        path = self.manifest_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def write_manifest(self, **updates) -> dict:
        data = self.read_manifest()
        data.update(updates)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data.setdefault("source", self.source)
        data.setdefault("job", self.path.name)
        atomic_write_text(self.manifest_path(), json.dumps(data, ensure_ascii=False, indent=2))
        return data

    def mark(self, step: str, ok: bool = True, detail: str = "") -> None:
        """Record a completed (or failed) step. Survives a crash; read back on resume."""
        data = self.read_manifest()
        steps = data.get("steps") or {}
        steps[step] = {"ok": ok, "detail": detail[:400], "at": time.time()}
        data["steps"] = steps
        data["state"] = "failed" if not ok else data.get("state", "running")
        atomic_write_text(self.manifest_path(), json.dumps(data, ensure_ascii=False, indent=2))

    def finished(self, **extra) -> None:
        data = self.read_manifest()
        data.update(extra)
        data["state"] = "done"
        atomic_write_text(self.manifest_path(), json.dumps(data, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------------- cleanup
    def cleanup(self, keep: tuple[str, ...] = ()) -> list[str]:
        """Delete intermediates, inside the job directory only.

        `keep` is a tuple of names (files or directories) to leave alone. Every
        candidate is re-checked with `contains` before deletion, so a symlink or a
        crafted name cannot turn this into an `rm -rf` somewhere else.
        """
        removed: list[str] = []
        for name in ("tts", "stems"):
            target = self.path / name
            if name in keep or not target.exists():
                continue
            if not self.contains(target):
                self.notes.append(f"cleanup skipped {name}: outside the job directory")
                continue
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
        for pattern in ("*.part*.wav", "*.stretch.wav"):
            for target in self.path.glob(pattern):
                if not self.contains(target):
                    continue
                target.unlink(missing_ok=True)
                removed.append(target.name)
        return removed

    # ------------------------------------------------------------------- lock
    def release(self) -> None:
        if self._lock is not None:
            with contextlib.suppress(OSError):
                self._lock.unlink()
            self._lock = None

    def __enter__(self) -> JobWorkspace:
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write via a sibling temp file + os.replace: readers never see half a file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def _try_lock(directory: Path) -> tuple[bool, Path]:
    """Create the lock file exclusively. Returns (acquired, lock_path)."""
    lock = directory / ".lock"
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()})
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False, lock
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return True, lock


def _lock_is_stale(lock: Path) -> bool:
    try:
        raw = lock.read_text(encoding="utf-8")
        info = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        info = {}
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True
    if age > STALE_AFTER_S:
        return True
    pid = info.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and not _pid_alive(pid):
        return True
    return not info  # unreadable or empty: nobody is holding this


def open_job(
    workdir: str | Path,
    source: str | Path,
    fingerprint: str = "",
    cache_key: str = "",
) -> JobWorkspace:
    """Prepare a locked job directory for `source`, reusing an interrupted one.

    * same path, previous run finished or crashed → same directory, resumed
    * same path, another live process → a new `-2`, `-3` … directory
    * different path, same filename → a different directory (the path hash differs)
    * same path, different *content* → the old artefacts are cleared, because they
      belong to a file that no longer exists
    """
    source = str(source)
    root = Path(workdir)
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    cache_dir = root / "cache" / (cache_key or "no-key")
    cache_dir.mkdir(parents=True, exist_ok=True)

    base = jobs_root / f"{slug(Path(source).stem)}-{path_hash(source)}"
    for attempt in range(MAX_JOB_SUFFIXES):
        candidate = base if attempt == 0 else base.with_name(f"{base.name}-{attempt + 1}")
        candidate.mkdir(parents=True, exist_ok=True)
        acquired, lock = _try_lock(candidate)
        if acquired:
            workspace = JobWorkspace(
                path=candidate,
                cache_dir=cache_dir,
                source=source,
                fingerprint=fingerprint,
                reused=attempt == 0,
                _lock=lock,
            )
            _prepare(workspace)
            return workspace
        if _lock_is_stale(lock):
            lock.unlink(missing_ok=True)
            acquired, lock = _try_lock(candidate)
            if acquired:
                workspace = JobWorkspace(
                    path=candidate,
                    cache_dir=cache_dir,
                    source=source,
                    fingerprint=fingerprint,
                    reused=True,
                    _lock=lock,
                )
                workspace.notes.append("reclaimed a stale lock from an interrupted run")
                _prepare(workspace)
                return workspace
    raise WorkspaceError(
        f"{MAX_JOB_SUFFIXES} concurrent jobs are already using {base} — is another dub "
        "running on this file? Wait for it, or pass a different --workdir."
    )


def _prepare(workspace: JobWorkspace) -> None:
    """Clear artefacts that belong to different content, then record the new job."""
    manifest = workspace.read_manifest()
    previous = manifest.get("fingerprint")
    if previous and workspace.fingerprint and previous != workspace.fingerprint:
        for name in ("transcript.json", "dub.wav", "dub_norm.wav", "mix.wav"):
            (workspace.path / name).unlink(missing_ok=True)
        shutil.rmtree(workspace.path / "tts", ignore_errors=True)
        shutil.rmtree(workspace.path / "stems", ignore_errors=True)
        workspace.notes.append("source content changed: cleared the previous run's artefacts")
        manifest = {}
    elif (
        previous
        and workspace.fingerprint
        and previous == workspace.fingerprint
        and manifest.get("state") != "done"
    ):
        workspace.notes.append("resuming an interrupted run from this directory")
    workspace.write_manifest(
        fingerprint=workspace.fingerprint,
        source=workspace.source,
        state=manifest.get("state") if manifest.get("state") == "done" else "running",
        steps=manifest.get("steps", {}),
        cache=str(workspace.cache_dir),
    )
