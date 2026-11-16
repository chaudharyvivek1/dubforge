"""The data model a dub moves through: words → segments → rendered segments.

Everything is a plain dataclass with `to_dict`, so any stage can be dumped to JSON,
diffed, cached, or handed to another agent without re-reading a video.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Segment:
    """One line of dialogue, with everything learned about it along the way."""

    index: int
    start: float
    end: float
    text: str
    speaker: str = "SPEAKER_00"
    words: list[Word] = field(default_factory=list)
    # translation stage
    target_text: str | None = None
    translator: str | None = None
    rewrites: int = 0
    # voice stage
    voice: str | None = None
    style: str | None = None
    audio_path: str | None = None
    # timing stage
    window: float | None = None  # seconds available before the next line
    raw_duration: float | None = None  # TTS output before stretching
    rendered_duration: float | None = None
    speed: float = 1.0  # atempo factor actually applied
    # bookkeeping
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def speech(self) -> str:
        """What will actually be spoken: the translation if there is one."""
        return self.target_text if self.target_text is not None else self.text

    @property
    def overflow(self) -> float:
        if self.rendered_duration is None or self.window is None:
            return 0.0
        return max(0.0, self.rendered_duration - self.window)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["words"] = [w.to_dict() for w in self.words]
        data["duration"] = round(self.duration, 3)
        data["overflow"] = round(self.overflow, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Segment:
        data = dict(data)
        words = [Word(**w) for w in data.pop("words", [])]
        for key in ("duration", "overflow", "speech"):
            data.pop(key, None)
        return cls(words=words, **data)


@dataclass
class Transcript:
    segments: list[Segment]
    language: str = "unknown"
    provider: str = "unknown"
    speakers: list[str] = field(default_factory=list)
    duration: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "provider": self.provider,
            "speakers": self.speakers,
            "duration": round(self.duration, 3),
            "notes": self.notes,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Transcript:
        return cls(
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            language=data.get("language", "unknown"),
            provider=data.get("provider", "unknown"),
            speakers=data.get("speakers", []),
            duration=data.get("duration", 0.0),
            notes=data.get("notes", []),
        )

    def save(self, path: str | Path) -> str:
        """Write atomically: a crash mid-save must not leave a half-written transcript."""
        from .workspace import atomic_write_text

        return str(
            atomic_write_text(path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        )

    @classmethod
    def load(cls, path: str | Path) -> Transcript:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def word_count(self) -> int:
        return sum(len(s.speech.split()) for s in self.segments)


CHUNK_SIZE = 1 << 20  # 1 MiB: streaming, so a 4 GB film never lands in memory


def content_hash(path: str | Path, chunk_size: int = CHUNK_SIZE) -> str:
    """SHA-256 of the file's bytes, read in chunks.

    This is the only thing that reliably identifies "the same video". A name, a size
    and an mtime are three lies waiting to happen: two exports of the same lesson can
    be byte-identical with different names, and two different cuts can share a name,
    a size and a timestamp to the second.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_fingerprint(path: str | Path, extra: str = "", use_content: bool = True) -> str:
    """Cache key for a media file plus whatever stage settings produced the entry.

    Content hash + size + `extra` (provider, model, language). The file *name* is
    deliberately excluded: renaming or moving a file must not invalidate a paid
    transcription, and including it would only add collisions between directories.

    `use_content=False` falls back to size + mtime, for callers that need speed on a
    huge file and accept the risk; nothing in the pipeline uses that path.
    """
    p = Path(path)
    stat = p.stat()
    if use_content:
        identity = f"sha256:{content_hash(p)}"
    else:
        identity = f"meta:{stat.st_size}:{stat.st_mtime_ns}"
    raw = f"{identity}:{stat.st_size}:{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
