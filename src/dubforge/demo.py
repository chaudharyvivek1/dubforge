"""`dubforge demo` — the whole pipeline, offline, in one command.

No API key, no GPU, no network. It builds a short synthetic clip — centre-panned
"dialogue" bursts over a stereo music bed — hands the pipeline a script, and runs
every stage for real: separation, timing, the translation pass, per-line synthesis,
atempo fitting, assembly, loudness normalisation, ducked mix, mux and QC. The
output is a genuine mp4 with a genuine second audio track.

What it cannot show is *quality*, and it says so: the "voice" is a tone and the
"translator" passes the text through. QC marks both, which is exactly how a demo
should behave — it proves the plumbing, not the product.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ffmpeg, pipeline
from .config import Settings

# Three lines, two speakers, with real gaps between them. Lengths are chosen so a
# natural speaking rate fits each window, so the demo passes its own timing QC.
DEMO_SCRIPT = """\
SPEAKER_00: This is a dubbing test for the lab.
SPEAKER_01: Every line keeps its place in time.
SPEAKER_00: The music bed stays under the new voice.
"""

# (start, length, frequency) — the "dialogue", panned centre.
BURSTS = [(0.30, 2.40, 220.0), (3.30, 1.90, 330.0), (5.80, 2.60, 220.0)]
TOTAL = 9.2


def build_bed(path: str | Path, seconds: float = TOTAL) -> str:
    """A quiet music bed that lives entirely in the side channel (L = -R).

    Anti-phase is not a trick for the demo's benefit: it is what lets the mid/side
    separator lift a bed away from centre-panned dialogue, and what lets the stub
    ASR find speech boundaries on the centre channel alone. Real stereo soundtracks
    behave the same way, just less perfectly.
    """
    tone = "0.05*(sin(2*PI*196*t)+sin(2*PI*294*t))"
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={tone}|-{tone}:s=48000:d={seconds}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return str(path)


def build_clip(path: str | Path) -> str:
    """A 9-second test clip: colour bars, centre dialogue bursts, stereo music bed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    voice = path.with_suffix(".voice.wav")
    bed = path.with_suffix(".bed.wav")
    mixed = path.with_suffix(".mix.wav")
    ffmpeg.write_tone_sequence(voice, BURSTS, TOTAL, 48000)
    build_bed(bed)
    ffmpeg.run(
        [
            "-i",
            str(voice),
            "-i",
            str(bed),
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:normalize=0,alimiter=limit=0.95[m]",
            "-map",
            "[m]",
            "-c:a",
            "pcm_s16le",
            str(mixed),
        ]
    )
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=854x480:rate=25:duration={TOTAL}",
            "-i",
            str(mixed),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(path),
        ]
    )
    for temp in (voice, bed, mixed):
        Path(temp).unlink(missing_ok=True)
    return str(path)


def run(out_dir: str | Path = "dubforge-demo", target_lang: str = "hi", log=None) -> dict:
    """Build the clip and dub it with the offline providers."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clip = build_clip(out / "demo-source.mp4")

    settings = Settings(
        source_lang="en",
        target_lang=target_lang,
        asr="stub",
        translator="stub",
        tts="stub",
        separator="ffmpeg_center",
        lipsync="none",
        script=DEMO_SCRIPT,
        workdir=str(out / "work"),
        keep_background=True,
        context="a short technical demonstration",
    )
    result = pipeline.dub(clip, settings, output=out / f"demo-dubbed.{target_lang}.mp4", log=log)
    payload = {
        "source": clip,
        "output": result.output,
        "qc_ok": result.ok,
        "qc": result.qc,
        "steps": [s.to_dict() for s in result.steps],
        "subtitles": result.subtitles,
        "report": result.report,
        "note": "stub providers: the voice is a tone and the translation is a pass-through. "
        "The timings, the mix and the QC are real.",
    }
    (out / "demo-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload
