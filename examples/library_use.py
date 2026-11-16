#!/usr/bin/env python3
"""Using dubforge as a library rather than a CLI.

Three patterns worth copying: plan before spending, refuse to ship a failed QC, and
re-voice from an existing transcript so recognition is never paid for twice.
"""

from pathlib import Path

from dubforge import Settings, dub, plan
from dubforge.media import Transcript


def estimate_then_dub(video: str, target: str) -> dict:
    settings = Settings(target_lang=target, tts="sarvam", translator="openai", asr="deepgram")

    p = plan(video, settings)
    if not p.ready:
        raise SystemExit("not ready:\n  - " + "\n  - ".join(p.problems))
    print(f"{p.media['duration']:.0f}s of media, about ${p.estimates['usd']} to dub")

    result = dub(video, settings, log=print)
    if not result.ok:
        raise SystemExit(f"QC failed: {result.qc['failures']}")
    return result.to_dict()


def re_voice_from_transcript(transcript_json: str, video: str, target: str) -> str:
    """Second pass with a different voice: no ASR, no translation, no new bill for them."""
    transcript = Transcript.load(transcript_json)
    for segment in transcript.segments:
        segment.target_text = segment.target_text or segment.text
        segment.audio_path = None
        segment.rendered_duration = None

    settings = Settings(
        target_lang=target,
        asr="transcript",
        transcript=transcript_json,
        translator="stub",   # already translated; nothing to do
        tts="elevenlabs",
        voice_pool=["<voice-id>"],
    )
    result = dub(video, settings, log=print)
    return result.output


def fix_lines_then_render(transcript_json: str, video: str, edits: dict[int, str], target: str) -> str:
    """Hand-edit the translation, then render. Cheapest way to a good dub."""
    path = Path(transcript_json)
    transcript = Transcript.load(path)
    for segment in transcript.segments:
        if segment.index in edits:
            segment.target_text = edits[segment.index]
    fixed = transcript.save(path.with_name("transcript.edited.json"))

    settings = Settings(target_lang=target, asr="transcript", transcript=fixed, tts="sarvam")
    return dub(video, settings, log=print).output


if __name__ == "__main__":
    print(estimate_then_dub("lesson.mp4", "hi"))
