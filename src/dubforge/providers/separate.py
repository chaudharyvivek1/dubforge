"""Keeping the music and the effects: stem separation.

A dub that throws away the original audio bed sounds like a phone call from a
quiet room. Separation is what lets the new dialogue sit under the original score,
the laugh track and the door slam. Two adapters: a real source separator if you
have one, and an honest crude fallback that at least ducks the centre channel.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .. import ffmpeg
from .base import ProviderError, Separator, Spec


class DemucsSeparator(Separator):
    spec = Spec(
        name="demucs",
        kind="separator",
        title="Demucs htdemucs (local, two stems)",
        packages=("demucs",),
        capabilities=("vocals", "instrumental", "offline"),
        languages="n/a",
        verified=False,
        quality="Best open option: removes the dialogue from the bed convincingly, and keeps the music intact. "
        "Needs a few GB of disk for weights and CPU time (or a GPU).",
        notes="pip install demucs. Runs `demucs --two-stems=vocals`. GPU is used automatically if torch sees one.",
        docs="https://github.com/adefossez/demucs",
    )

    def separate(self, audio: str, out_dir: str) -> tuple[str | None, str | None]:
        self.require()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        cmd = ["-m", "demucs", "--two-stems=vocals", "-o", str(out), str(audio)]
        binary = shutil.which("demucs")
        full = [binary, *cmd[2:]] if binary else ["python", *cmd]
        proc = subprocess.run(full, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0:
            raise ProviderError(
                "demucs failed: " + "\n".join((proc.stderr or "").strip().splitlines()[-6:])
            )
        stem_dir = next((p for p in out.rglob("vocals.wav")), None)
        if not stem_dir:
            raise ProviderError("demucs finished but produced no vocals.wav")
        vocals = stem_dir
        bed = vocals.parent / "no_vocals.wav"
        return str(vocals), str(bed) if bed.exists() else None


class FFmpegCenterSeparator(Separator):
    spec = Spec(
        name="ffmpeg_center",
        kind="separator",
        title="Mid/side centre removal (crude, always available)",
        capabilities=("stereo_only", "fast"),
        languages="n/a",
        verified=True,
        quality="Splits mid from side. Keeps the stereo music bed and most of the dialogue is removed, "
        "but the original voice bleeds through and mono sources cannot be separated at all.",
        notes="Zero dependencies, instant. Use demucs when quality matters; this is the fallback that "
        "still beats replacing the whole soundtrack.",
    )

    def separate(self, audio: str, out_dir: str) -> tuple[str | None, str | None]:
        info = ffmpeg.probe(audio)
        if info.channels < 2:
            return None, None
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        bed = out / "bed.wav"
        ffmpeg.run(
            [
                "-i",
                str(audio),
                "-af",
                "pan=stereo|c0=c0-c1|c1=c1-c0,highpass=f=40",
                str(bed),
            ]
        )
        return None, str(bed)


class NoneSeparator(Separator):
    spec = Spec(
        name="none",
        kind="separator",
        title="No separation",
        verified=True,
        quality="The original audio becomes the bed, dialogue included. Only right when the source is "
        "clean narration or a single speaker over silence.",
    )

    def separate(self, audio: str, out_dir: str) -> tuple[str | None, str | None]:
        return None, None
