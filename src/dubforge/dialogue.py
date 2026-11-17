"""Partial remixing: splice changed lines into the dialogue track instead of rebuilding it.

Assembling the dialogue track is not free — every clip becomes an `adelay`/`amix` chain, and
a 300-line film is several passes over the whole audio. 0.4.0 reused the renders and the bed
but still rebuilt this track from scratch, so "five lines edited" still cost a full assembly.

This module keeps the assembled track with a per-line manifest, and on a regeneration it
**mutes only the changed lines' own time ranges and mixes the new renders in at the same
starts** — one ffmpeg pass, independent of how long the film is.

The rules that keep it honest:

* A line's placement is `[start, start + its own rendered duration]`. Only that range is
  muted, so a neighbour's audio in an overlapping window is never cut.
* **If a changed line overlaps another line, the splice is refused** and the caller falls
  back to a full assembly: zeroing a shared time range would delete the other speaker.
* The track carries a SHA-256; a truncated or replaced file is a miss, never a quiet splice
  onto the wrong audio.
* Splicing reuses *placement*, not processing: ducking, loudness normalisation, the mix and
  the mux all still run, because they depend on the current dialogue signal.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg


@dataclass
class DialogueEntry:
    seg_id: str
    start: float
    duration: float
    speed: float = 1.0
    clip_sha256: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> DialogueEntry:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class DialogueTrack:
    path: str
    sha256: str
    total_duration: float
    sample_rate: int = 48000
    entries: list[DialogueEntry] = field(default_factory=list)

    def by_id(self) -> dict[str, DialogueEntry]:
        return {entry.seg_id: entry for entry in self.entries}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class DialogueStore:
    """`<work>/dialogue.wav` + `dialogue.json`, written atomically."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def audio(self) -> Path:
        return self.root / "dialogue.wav"

    @property
    def meta(self) -> Path:
        return self.root / "dialogue.json"

    def find(self) -> DialogueTrack | None:
        if not self.meta.exists() or not self.audio.exists():
            return None
        try:
            payload = json.loads(self.meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        entries = [DialogueEntry.from_dict(row) for row in payload.get("entries", [])]
        track = DialogueTrack(
            path=str(self.audio),
            sha256=payload.get("sha256", ""),
            total_duration=float(payload.get("total_duration", 0.0)),
            sample_rate=int(payload.get("sample_rate", 48000)),
            entries=entries,
        )
        if not track.sha256 or track.total_duration <= 0:
            return None
        try:
            if sha256_file(self.audio) != track.sha256:
                return None  # a truncated or replaced track is a miss
        except OSError:
            return None
        return track

    def save(
        self,
        source: str | Path,
        entries: list[DialogueEntry],
        total_duration: float,
        sample_rate: int = 48000,
    ) -> DialogueTrack:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.audio.with_suffix(".wav.tmp")
        temp.write_bytes(Path(source).read_bytes())
        os.replace(temp, self.audio)
        track = DialogueTrack(
            path=str(self.audio),
            sha256=sha256_file(self.audio),
            total_duration=total_duration,
            sample_rate=sample_rate,
            entries=entries,
        )
        payload = {
            "sha256": track.sha256,
            "total_duration": total_duration,
            "sample_rate": sample_rate,
            "entries": [entry.to_dict() for entry in entries],
        }
        temp_meta = self.meta.with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp_meta, self.meta)
        return track


def overlaps_any(
    entry: DialogueEntry, others: list[DialogueEntry], tolerance: float = 0.01
) -> bool:
    """Whether this line's audio shares time with another line's audio."""
    start, end = entry.start, entry.start + entry.duration
    for other in others:
        if other.seg_id == entry.seg_id:
            continue
        other_start = other.start
        other_end = other.start + other.duration
        if start < other_end - tolerance and other_start < end - tolerance:
            return True
    return False


def splice(
    base: str | Path,
    dst: str | Path,
    replacements: list[tuple[float, float, str | Path]],
    total_duration: float,
    sample_rate: int = 48000,
) -> str:
    """Mute the given ranges in `base` and mix new clips in at their starts — one pass.

    `replacements` is `(start, old_duration, new_clip)`. Only `[start, start + old_duration]`
    is muted, which is exactly the extent the old render occupied.
    """
    base, dst = Path(base), Path(dst)
    if not replacements:
        raise ValueError("nothing to splice")
    inputs = ["-i", str(base)]
    chains: list[str] = []
    labels: list[str] = []

    mutes = []
    for start, old_duration, _ in replacements:
        begin = max(0.0, start)
        end = max(begin, start + max(0.0, old_duration))
        mutes.append(f"volume=enable='between(t,{begin:.3f},{end:.3f})':volume=0")
    chains.append(
        "[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo," + ",".join(mutes) + "[base]"
    )
    labels.append("[base]")

    for index, (start, _, clip) in enumerate(replacements, start=1):
        inputs += ["-i", str(clip)]
        ms = max(0, int(round(start * 1000)))
        chains.append(
            f"[{index}:a]aresample={sample_rate},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,adelay={ms}:all=1[n{index}]"
        )
        labels.append(f"[n{index}]")

    graph = ";".join(chains) + ";" + "".join(labels)
    graph += f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,apad[mix]"
    ffmpeg.run(
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[mix]",
            "-t",
            f"{max(total_duration, 0.1):.3f}",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    return str(dst)
