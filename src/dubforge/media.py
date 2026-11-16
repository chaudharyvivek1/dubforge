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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(path)

    @classmethod
    def load(cls, path: str | Path) -> Transcript:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def word_count(self) -> int:
        return sum(len(s.speech.split()) for s in self.segments)


def file_fingerprint(path: str | Path, extra: str = "") -> str:
    """Stable cache key: file size + mtime + stage settings (never the whole file)."""
    p = Path(path)
    stat = p.stat()
    raw = f"{p.name}:{stat.st_size}:{int(stat.st_mtime)}:{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
