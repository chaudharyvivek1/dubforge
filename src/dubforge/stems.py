"""Source-stem cache: separate once, reuse the bed for every regeneration.

Separation is the most expensive local stage in the pipeline — a Demucs pass on a long film
is minutes of CPU — and it depends only on the source audio and the separator, never on the
dialogue. Regenerating five lines used to pay for it again. This cache does not.

What is cached, and what is deliberately not:

* **cached**: the background/music/effects bed, plus a small metadata record;
* **not cached**: the extracted 48 kHz source audio (cheap to redo, and duplicating a film's
  audio in the cache is a disk cost with no benefit) and the **final mix** (its ducking
  envelope depends on the current dialogue, so reusing it would be a lie — see
  `docs/architecture.md`).

The key covers everything that changes the bed: source content hash, separator name, the
separator's model/version if it reports one, the keep-background setting and the output
sample rate. A different separator or a re-encoded source is a miss, never a wrong bed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LOCK_STALE_S = 600.0


@dataclass
class StemRecord:
    key: str
    bed: str
    bed_sha256: str
    duration_s: float = 0.0
    source_hash: str = ""
    separator: str = ""
    model: str = ""
    sample_rate: int = 48000
    created_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> StemRecord:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def cache_key(
    source_hash: str,
    separator: str,
    *,
    model: str = "",
    keep_background: bool = True,
    sample_rate: int = 48000,
) -> str:
    payload = {
        "source": source_hash,
        "separator": separator,
        "model": model,
        "keep_background": keep_background,
        "sample_rate": sample_rate,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class StemCache:
    """`<workdir>/cache/stems/<key>/bed.wav` + `meta.json`, written atomically."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def dir(self, key: str) -> Path:
        return self.root / key

    def bed_path(self, key: str) -> Path:
        return self.dir(key) / "bed.wav"

    def meta_path(self, key: str) -> Path:
        return self.dir(key) / "meta.json"

    # ------------------------------------------------------------------ lookup
    def find(self, key: str) -> StemRecord | None:
        """A bed is reused only if it is present and its checksum still matches."""
        meta = self.meta_path(key)
        if not meta.exists():
            return None
        try:
            record = StemRecord.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None
        bed = self.bed_path(key)
        if not bed.exists() or bed.stat().st_size < 1024:
            return None
        try:
            if sha256_file(bed) != record.bed_sha256:
                return None  # a truncated or replaced bed is a miss, not a quiet mistake
        except OSError:
            return None
        record.bed = str(bed)
        return record

    # ------------------------------------------------------------------- write
    @contextlib.contextmanager
    def claim(self, key: str, wait: float = 0.0, poll: float = 0.1):
        """Exclusive ownership while separating: two workers must not both run Demucs."""
        lock = self.dir(key).with_suffix(".lock")
        self.root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:12]
        deadline = time.time() + max(0.0, wait)
        acquired = False
        while True:
            fd = None
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                acquired = True
            except FileExistsError:
                if self._stale(lock):
                    with contextlib.suppress(OSError):
                        lock.unlink()
                    continue
                if time.time() >= deadline:
                    break
                time.sleep(poll)
            except OSError:
                break
            if acquired and fd is not None:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"pid": os.getpid(), "token": token, "at": time.time()})
                    )
            break
        try:
            yield acquired
        finally:
            if acquired:
                with contextlib.suppress(OSError, json.JSONDecodeError):
                    holder = json.loads(lock.read_text(encoding="utf-8"))
                    if holder.get("token") == token:
                        lock.unlink()

    @staticmethod
    def _stale(lock: Path) -> bool:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return False
        try:
            holder = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return age > 5.0
        pid = holder.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and os.name != "nt":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except (PermissionError, OSError):
                return False
            return False
        return age > LOCK_STALE_S

    def save(self, record: StemRecord, bed_source: str | Path) -> StemRecord:
        """Copy the bed in and write its record, atomically, in that order."""
        directory = self.dir(record.key)
        directory.mkdir(parents=True, exist_ok=True)
        target = self.bed_path(record.key)
        temp = target.with_suffix(".wav.tmp")
        shutil.copyfile(bed_source, temp)
        os.replace(temp, target)

        record.bed_sha256 = sha256_file(target)
        stored = {**record.to_dict(), "bed": target.name}
        temp_meta = self.meta_path(record.key).with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        os.replace(temp_meta, self.meta_path(record.key))
        record.bed = str(target)
        return record

    def restore(self, record: StemRecord, destination: str | Path) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(record.bed, destination)
        return str(destination)

    # ------------------------------------------------------------- housekeeping
    def stats(self) -> dict:
        entries = 0
        total = 0
        for directory in self.root.glob("*"):
            if not directory.is_dir():
                continue
            entries += 1
            for path in directory.rglob("*"):
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        return {"entries": entries, "bytes": total, "root": str(self.root)}

    def cleanup(
        self,
        older_than_days: int | None = None,
        max_bytes: int | None = None,
        dry_run: bool = True,
        keep: tuple[str, ...] = (),
    ) -> dict:
        """Drop old entries, then the largest-first until under `max_bytes`."""
        cutoff = time.time() - (older_than_days or 0) * 86400 if older_than_days else None
        candidates: list[tuple[float, int, str]] = []
        for meta in self.root.glob("*/meta.json"):
            key = meta.parent.name
            if key in keep:
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                created = float(payload.get("created_at") or meta.stat().st_mtime)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                created = meta.stat().st_mtime
            size = sum(p.stat().st_size for p in meta.parent.rglob("*") if p.is_file())
            candidates.append((created, size, key))

        removed: list[str] = []
        total = sum(size for _, size, _ in candidates)
        for created, size, key in sorted(candidates):
            too_old = cutoff is not None and created < cutoff
            too_big = max_bytes is not None and total > max_bytes
            if not (too_old or too_big):
                continue
            removed.append(key)
            total -= size
            if not dry_run:
                shutil.rmtree(self.dir(key), ignore_errors=True)
        return {
            "removed": removed,
            "kept": len(candidates) - len(removed),
            "bytes_after": total,
            "dry_run": dry_run,
        }
