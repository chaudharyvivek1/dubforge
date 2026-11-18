"""Per-segment render cache: regenerate one line, not the whole film.

Changing one translated sentence used to mean paying for every sentence again. This module
stores each line's **raw render** (before the atempo stretch) beside a fingerprint of
everything that changes it, so a re-run synthesises only the lines whose inputs moved.

Two decisions worth knowing:

* **The raw render is cached, not the stretched one.** The stretch depends on the *window*,
  which changes when a neighbouring line changes. Re-running atempo is milliseconds and
  free; re-running TTS is neither. So the cache key covers the things that change the voice
  output (text, voice, provider, model, language, style, emotion, sample rate) and
  deliberately not the timing, which is re-applied every run.
* **An artifact is only reused if it is provably intact.** The record carries the SHA-256 of
  the audio file; a truncated or replaced file is a miss, not a silent hole in the dub.

Safety: writes are atomic (temp + `os.replace`), a per-segment lock stops two workers
writing the same line, and an interrupted run leaves at worst an orphaned temp file that the
next run ignores. Nothing here touches a previous *output* — the mix and the mux are built
elsewhere, and a failed regeneration cannot overwrite the file it was replacing.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .media import Segment

# The inputs that change what the voice says, and nothing else.
FINGERPRINT_FIELDS = (
    "target_text",
    "voice",
    "style",
    "emotion",
    "provider",
    "model",
    "language",
    "sample_rate",
)

LOCK_STALE_S = 120.0


def _can_probe_pid() -> bool:
    """Whether a pid means anything here. On Windows it does not (see jobs.py)."""
    return os.name != "nt"


@dataclass
class RenderRecord:
    seg_id: str
    fingerprint: str
    audio: str
    audio_sha256: str
    duration_s: float
    provider: str = ""
    voice: str = ""
    model: str = ""
    language: str = ""
    target_text: str = ""
    style: str = ""
    emotion: str = ""
    sample_rate: int = 0
    created_at: float = field(default_factory=time.time)
    timing: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> RenderRecord:
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})


def segment_id(segment: Segment) -> str:
    """A stable id: its position plus what identifies the line, not its current text.

    Position alone would collide when a line is split; text alone would change identity the
    moment somebody edits it — which is exactly when the cache must be consulted, not reset.
    """
    basis = f"{segment.index}|{segment.speaker}|{round(segment.start, 2)}"
    return f"{segment.index:05d}-{hashlib.sha256(basis.encode()).hexdigest()[:8]}"


def fingerprint(
    segment: Segment, *, provider: str, model: str, language: str, sample_rate: int
) -> str:
    """Hash of everything that changes the rendered audio."""
    payload = {
        "target_text": segment.speech,
        "voice": segment.voice or "",
        "style": segment.style or "",
        "emotion": getattr(segment, "emotion", "") or "",
        "provider": provider,
        "model": model,
        "language": language,
        "sample_rate": sample_rate,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class SegmentStore:
    """`<job_dir>/segments/<seg_id>.wav` + `<seg_id>.json`, written atomically."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def audio_path(self, seg_id: str) -> Path:
        return self.dir() / f"{seg_id}.wav"

    def meta_path(self, seg_id: str) -> Path:
        return self.dir() / f"{seg_id}.json"

    def placed_path(self, seg_id: str) -> Path:
        """The render *after* the window-specific stretch, when it is still valid."""
        return self.dir() / f"{seg_id}.placed.wav"

    # ------------------------------------------------------------------ lookup
    def find(self, seg_id: str, expected: str) -> RenderRecord | None:
        """The record for this segment *if* its fingerprint matches and the audio is intact."""
        meta = self.meta_path(seg_id)
        if not meta.exists():
            return None
        try:
            record = RenderRecord.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None
        if record.fingerprint != expected:
            return None
        audio = Path(record.audio)
        if not audio.is_absolute():
            audio = self.root / audio
        if not audio.exists() or audio.stat().st_size == 0:
            return None
        try:
            if sha256_file(audio) != record.audio_sha256:
                return None  # a truncated or replaced file is a miss, never a silent hole
        except OSError:
            return None
        record.audio = str(audio)
        return record

    # ------------------------------------------------------------------- write
    @contextlib.contextmanager
    def claim(self, seg_id: str, wait: float = 0.0, poll: float = 0.05):
        """Exclusive ownership of one segment, with a token, for the whole synthesis.

        The point is to own the segment **before** calling a paid provider, then re-check the
        cache under that ownership: two workers that both miss must not both synthesise.

        Waiting is bounded (`wait` seconds). A lock is stolen only when its *owner* is gone —
        a dead pid on POSIX, or an age past `LOCK_STALE_S` where a pid cannot be probed — so a
        slow-but-live worker is never robbed mid-render. If the wait runs out the caller gets
        `False` and renders without the cache, which is slower but never wrong.
        """
        lock = self.dir() / f"{seg_id}.lock"
        token = uuid.uuid4().hex[:12]
        deadline = time.time() + max(0.0, wait)
        acquired = False
        while True:
            fd = None
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                acquired = True
            except FileExistsError:
                if self._lock_is_stale(lock):
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
                # Only release our own lock: a stolen one belongs to somebody else now.
                with contextlib.suppress(OSError, json.JSONDecodeError):
                    holder = json.loads(lock.read_text(encoding="utf-8"))
                    if holder.get("token") == token:
                        lock.unlink()

    @staticmethod
    def _lock_is_stale(lock: Path) -> bool:
        """A lock is stale when its *owner* is gone, not merely when it is old.

        The pid is checked first on purpose. An age check that ran first would let a second
        worker steal the lock from a slow-but-live renderer once it crossed the threshold —
        two paid syntheses for one line, which is the bug this whole mechanism exists to
        prevent. Age is only the fallback where a pid cannot be probed (Windows).
        """
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return False  # vanished between the open and the stat: retry the open
        try:
            holder = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Created but not yet written: live unless it has been that way for a while.
            return age > 5.0
        pid = holder.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and _can_probe_pid():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except (PermissionError, OSError):
                return False
            return False
        return age > LOCK_STALE_S

    def save(self, record: RenderRecord, audio_source: str | Path) -> RenderRecord:
        """Copy the render in and write its record — atomically, in that order."""
        self.dir()
        audio_target = self.audio_path(record.seg_id)
        temp_audio = audio_target.with_suffix(".wav.tmp")
        temp_audio.write_bytes(Path(audio_source).read_bytes())
        os.replace(temp_audio, audio_target)

        record.audio_sha256 = sha256_file(audio_target)
        # On disk the path is relative to the store, so a work directory can be moved or
        # copied between machines. The caller gets the absolute path, because it is about to
        # read the file.
        stored = {**record.to_dict(), "audio": audio_target.name}
        temp_meta = self.meta_path(record.seg_id).with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        os.replace(temp_meta, self.meta_path(record.seg_id))
        record.audio = str(audio_target)
        return record

    def save_placed(
        self, seg_id: str, audio: str | Path, speed: float, duration: float | None = None
    ) -> None:
        """Remember the stretched render so a re-run with the same window skips atempo.

        The stretch is cheap per line but not free, and on a 300-line regeneration it is 300
        ffmpeg invocations. It is keyed on the speed actually applied, because a different
        window means a different stretch.
        """
        target = self.placed_path(seg_id)
        temp = target.with_suffix(".wav.tmp")
        temp.write_bytes(Path(audio).read_bytes())
        os.replace(temp, target)
        record = self._read(seg_id)
        if record is None:
            return
        record.meta = {
            **record.meta,
            "speed": round(float(speed), 3),
            "placed_sha256": sha256_file(target),
            "placed_duration": round(float(duration), 3) if duration else 0.0,
        }
        temp_meta = self.meta_path(seg_id).with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        os.replace(temp_meta, self.meta_path(seg_id))

    def find_placed(self, seg_id: str, speed: float) -> tuple[str, float] | None:
        """The stretched render and its measured duration, if the speed still matches."""
        record = self._read(seg_id)
        if record is None:
            return None
        stored = record.meta.get("speed")
        if stored is None or abs(float(stored) - round(float(speed), 3)) > 1e-6:
            return None
        placed = self.placed_path(seg_id)
        if not placed.exists():
            return None
        try:
            if sha256_file(placed) != record.meta.get("placed_sha256"):
                return None
        except OSError:
            return None
        # The duration is stored with the artifact, so reusing it costs no ffprobe call.
        return str(placed), float(record.meta.get("placed_duration") or 0.0)

    def _read(self, seg_id: str) -> RenderRecord | None:
        try:
            return RenderRecord.from_dict(
                json.loads(self.meta_path(seg_id).read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def forget(self, seg_id: str) -> None:
        for path in (self.audio_path(seg_id), self.meta_path(seg_id), self.placed_path(seg_id)):
            with contextlib.suppress(OSError):
                path.unlink()

    def stats(self) -> dict:
        records = 0
        bytes_total = 0
        for meta in self.dir().glob("*.json"):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                records += 1
            with contextlib.suppress(OSError):
                bytes_total += meta.stat().st_size
        return {"records": records, "bytes": bytes_total}


def restore(record: RenderRecord, destination: str | Path) -> str:
    """Copy a cached render to where the pipeline expects this line's audio."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(Path(record.audio).read_bytes())
    return str(destination)
