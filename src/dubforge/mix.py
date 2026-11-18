"""Stage-level reuse: the mix and the mux, cached against their real inputs.

After 0.5.0 a regeneration reused the renders, the separated bed and (spliced) the dialogue
track — then still normalised, ducked, mixed and muxed every time. On a film where nothing
changed at all, that was three audio passes and an encode to produce a byte-identical file.

This module closes the ladder. Each stage gets a key over exactly the inputs that determine
its output:

* **mix** — the dialogue track's content hash, the bed's hash, the voice and bed gains, the
  ducking switch, the loudness target, the true-peak ceiling, the runtime and the sample rate.
* **mux** — the mix's hash, the source's hash, the audio bitrate and whether the source has
  video.

What this deliberately does **not** do: reuse the mix for a *spliced* dialogue. Loudness
normalisation applies one gain to the whole track, so a changed line moves that gain and every
untouched region moves with it. Splicing the mix would be a lie about the audio, so a spliced
dialogue always rebuilds the mix — and the run report says `mix_mode: rebuilt`.

The distinction matters because it is the difference between "we cached it" and "we cached it
where the maths allows".
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_or_empty(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        return sha256_file(path)
    except OSError:
        return ""


def mix_key(
    dialogue: str | Path,
    bed: str | Path | None,
    *,
    voice_gain_db: float,
    bed_gain_db: float,
    duck: bool,
    loudness_lufs: float,
    true_peak_db: float,
    total_duration: float,
    sample_rate: int = 48000,
) -> str:
    """Everything that determines the mixed audio, and nothing else."""
    payload = {
        "dialogue": _hash_or_empty(dialogue),
        "bed": _hash_or_empty(bed),
        "voice_gain_db": round(float(voice_gain_db), 3),
        "bed_gain_db": round(float(bed_gain_db), 3),
        "duck": bool(duck),
        "loudness_lufs": round(float(loudness_lufs), 3),
        "true_peak_db": round(float(true_peak_db), 3),
        "total_duration": round(float(total_duration), 3),
        "sample_rate": int(sample_rate),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def mux_key(mixed: str | Path, source: str | Path, *, audio_bitrate: str, has_video: bool) -> str:
    """Everything that determines the container, and nothing else."""
    payload = {
        "mixed": _hash_or_empty(mixed),
        "source": _hash_or_empty(source),
        "audio_bitrate": str(audio_bitrate),
        "has_video": bool(has_video),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


@dataclass
class StageRecord:
    key: str
    artifact: str
    sha256: str
    suffix: str = ".wav"
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> StageRecord:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class StageCache:
    """`<root>/<key><suffix>` + `<key>.json`, written atomically, verified by checksum."""

    def __init__(self, root: str | Path, suffix: str = ".wav"):
        self.root = Path(root)
        self.suffix = suffix

    def artifact(self, key: str) -> Path:
        return self.root / f"{key}{self.suffix}"

    def meta(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def find(self, key: str) -> StageRecord | None:
        meta = self.meta(key)
        if not meta.exists():
            return None
        try:
            record = StageRecord.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None
        artifact = Path(record.artifact)
        if not artifact.is_absolute():
            artifact = self.root / artifact
        if not artifact.exists() or artifact.stat().st_size < 1024:
            return None
        try:
            if sha256_file(artifact) != record.sha256:
                return None  # a truncated or replaced artifact is a miss
        except OSError:
            return None
        record.artifact = str(artifact)
        return record

    def save(self, key: str, source: str | Path, meta: dict | None = None) -> StageRecord:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.artifact(key)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_bytes(Path(source).read_bytes())
        os.replace(temp, target)
        record = StageRecord(
            key=key,
            artifact=target.name,
            sha256=sha256_file(target),
            suffix=self.suffix,
            meta=meta or {},
        )
        temp_meta = self.meta(key).with_suffix(".json.tmp")
        temp_meta.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        os.replace(temp_meta, self.meta(key))
        record.artifact = str(target)
        return record

    def restore(self, record: StageRecord, destination: str | Path) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(record.artifact).read_bytes())
        return str(destination)

    def stats(self) -> dict:
        entries = 0
        total = 0
        if not self.root.exists():
            return {"entries": 0, "bytes": 0, "root": str(self.root)}
        for path in self.root.glob(f"*{self.suffix}"):
            entries += 1
            with_path = path
            try:
                total += with_path.stat().st_size
            except OSError:
                continue
        return {"entries": entries, "bytes": total, "root": str(self.root)}

    def cleanup(self, max_bytes: int | None = None, dry_run: bool = True) -> dict:
        """Drop the largest artifacts first until the cache fits `max_bytes`."""
        entries: list[tuple[int, Path]] = []
        if self.root.exists():
            for path in self.root.glob(f"*{self.suffix}"):
                try:
                    entries.append((path.stat().st_size, path))
                except OSError:
                    continue
        total = sum(size for size, _ in entries)
        removed: list[str] = []
        for size, path in sorted(entries, reverse=True):
            if max_bytes is None or total <= max_bytes:
                break
            removed.append(path.name)
            total -= size
            if not dry_run:
                for target in (path, path.with_suffix(".json")):
                    with contextlib.suppress(OSError):
                        target.unlink()
        return {"removed": removed, "bytes_after": total, "dry_run": dry_run}


def summarise_timings(steps) -> dict:
    """Per-stage seconds, and the slowest one, for the run report.

    A regeneration's cost is dominated by one or two stages; without this a user cannot tell
    whether they are paying for synthesis, separation or the mux.
    """
    seconds = {step.name: round(step.seconds, 2) for step in steps if step.seconds}
    total = round(sum(seconds.values()), 2)
    slowest = max(seconds, key=seconds.get) if seconds else ""
    return {
        "stage_seconds": seconds,
        "total_stage_seconds": total,
        "slowest_stage": slowest,
        "slowest_seconds": seconds.get(slowest, 0.0),
    }
