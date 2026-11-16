"""Subtitle I/O: the dub's transcript as something a human can check.

A dub nobody can proofread is a dub nobody should ship. Every run writes the
source transcript, the translation, and a bilingual SRT side by side.
"""

from __future__ import annotations

import re
from pathlib import Path

from .media import Segment


def _ts(seconds: float, sep: str = ",") -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _parse_ts(text: str) -> float:
    m = _TS_RE.search(text)
    if not m:
        raise ValueError(f"bad timestamp: {text!r}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def to_srt(segments: list[Segment], target: bool = True) -> str:
    blocks = []
    for n, seg in enumerate(segments, start=1):
        text = seg.speech if target else seg.text
        if not text.strip():
            continue
        blocks.append(f"{n}\n{_ts(seg.start)} --> {_ts(seg.end)}\n{text.strip()}\n")
    return "\n".join(blocks)


def to_bilingual_srt(segments: list[Segment]) -> str:
    """Source above, translation below — the fastest way to review a dub by eye."""
    blocks = []
    for n, seg in enumerate(segments, start=1):
        source = seg.text.strip()
        target = (seg.target_text or "").strip()
        body = source if not target else f"{source}\n{target}"
        if not body:
            continue
        blocks.append(f"{n}\n{_ts(seg.start)} --> {_ts(seg.end)}\n{body}\n")
    return "\n".join(blocks)


def to_vtt(segments: list[Segment], target: bool = True) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = seg.speech if target else seg.text
        if not text.strip():
            continue
        lines.append(f"{_ts(seg.start, '.')} --> {_ts(seg.end, '.')}")
        lines.append(text.strip())
        lines.append("")
    return "\n".join(lines)


def read_srt(path: str | Path) -> list[Segment]:
    """Parse an SRT you already have — lets you dub from a supplied transcript."""
    text = Path(path).read_text(encoding="utf-8-sig")
    segments: list[Segment] = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        timing = next((ln for ln in lines if "-->" in ln), None)
        if not timing:
            continue
        start_s, end_s = timing.split("-->")
        start, end = _parse_ts(start_s), _parse_ts(end_s)
        idx = lines.index(timing)
        body = " ".join(lines[idx + 1 :]).strip()
        segments.append(Segment(index=len(segments), start=start, end=end, text=body))
    return segments


def write_all(
    segments: list[Segment],
    out_dir: str | Path,
    stem: str,
    lang: str = "dub",
) -> dict[str, str]:
    """Write the four review files. `lang` names the target file, e.g. `lesson.hi.srt`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = (lang or "dub").strip() or "dub"
    paths = {
        "source_srt": out_dir / f"{stem}.source.srt",
        "target_srt": out_dir / f"{stem}.{tag}.srt",
        "bilingual_srt": out_dir / f"{stem}.bilingual.srt",
        "vtt": out_dir / f"{stem}.vtt",
    }
    paths["source_srt"].write_text(to_srt(segments, target=False), encoding="utf-8")
    paths["target_srt"].write_text(to_srt(segments, target=True), encoding="utf-8")
    paths["bilingual_srt"].write_text(to_bilingual_srt(segments), encoding="utf-8")
    paths["vtt"].write_text(to_vtt(segments), encoding="utf-8")
    return {key: str(value) for key, value in paths.items()}
