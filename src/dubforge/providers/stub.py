"""Offline providers: no key, no network, deterministic.

These exist for two reasons and both matter:

* `dubforge demo` and the test suite have to run on a school laptop with no API
  keys and no GPU, and still exercise the real pipeline — real ffmpeg, real
  timing, real mixing, a real mp4 at the end.
* a broken machine must be diagnosable. If a run works with `--asr stub` and
  fails with a real provider, the pipeline is fine and the provider is not.

They are **not** a translator or a voice. The stub "voice" is a tone, and the stub
translator returns the text unchanged and says so in the QC report, so nobody can
mistake a demo for a finished dub.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import ffmpeg
from ..fit import estimate_seconds
from ..media import Segment, Transcript, Word
from .base import ASRProvider, LipSync, Separator, Spec, SynthResult, Translator, TTSProvider, Voice

SPEAKER_PREFIX = re.compile(r"^\s*(?:(SPEAKER[_-]?\d+|[A-Z][A-Z0-9_]{1,20})\s*[:\-]\s*)")


def parse_script(text: str) -> list[tuple[str, str]]:
    """`SPEAKER_00: line` per line → [(speaker, line)]. Unlabelled lines alternate speakers."""
    rows: list[tuple[str, str]] = []
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SPEAKER_PREFIX.match(line)
        if match:
            rows.append((match.group(1).replace("-", "_").upper(), line[match.end() :].strip()))
        else:
            rows.append((f"SPEAKER_{i % 2:02d}", line))
    return rows


def even_timings(count: int, total: float) -> list[tuple[float, float]]:
    """Split `total` seconds into `count` slots, leaving a small gap between them."""
    if count <= 0:
        return []
    slot = total / count
    gap = min(0.25, slot * 0.2)
    return [(i * slot, i * slot + slot - gap) for i in range(count)]


class StubASR(ASRProvider):
    spec = Spec(
        name="stub",
        kind="asr",
        title="Script-driven fake recogniser (offline)",
        capabilities=("diarize", "word_timings"),
        languages="whatever you type",
        verified=True,
        quality="Not recognition. Segments come from the text you supply and the audio's own silences.",
        notes="Set DUBFORGE_SCRIPT to the transcript text, or use `dubforge demo`.",
    )

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        script = (self.settings.script or "").strip()
        if not script:
            raise ValueError(
                "the stub ASR needs a script. Pass --script 'SPEAKER_00: line one\\nSPEAKER_01: line two' "
                "or set DUBFORGE_SCRIPT. For real recognition use --asr faster_whisper / deepgram / elevenlabs."
            )
        rows = parse_script(script)
        total = ffmpeg.duration_of(audio)
        # Dialogue is mixed to the centre; the music bed sits in the sides. Detecting
        # silence on the centre channel is therefore far more reliable than on the mix.
        spans = ffmpeg.silence_spans(_centre_mono(audio), noise_db=-30, min_duration=0.3)

        # Prefer the audio's own speech islands: they make the demo's timings real.
        regions = _speech_islands(spans, total)
        # When the audio's own islands match the script line for line, use them: real
        # boundaries beat an even split. Otherwise fall back to proportional slots.
        timings = regions if len(regions) == len(rows) else even_timings(len(rows), total)

        segments: list[Segment] = []
        for i, ((speaker, text), (start, end)) in enumerate(zip(rows, timings, strict=False)):
            words = _spread_words(text, start, end, speaker)
            segments.append(
                Segment(
                    index=i,
                    start=round(start, 3),
                    end=round(end, 3),
                    text=text,
                    speaker=speaker if diarize else "SPEAKER_00",
                    words=words,
                )
            )
        return Transcript(
            segments=segments,
            language=language or "en",
            provider="stub",
            speakers=sorted({s.speaker for s in segments}),
            duration=total,
            notes=[
                "stub ASR: timings come from the script plus silence detection, not from recognition",
            ],
        )


def _centre_mono(audio: str) -> str:
    """Mono sum of the front pair: dialogue in, stereo music bed out."""
    try:
        info = ffmpeg.probe(audio)
    except ffmpeg.FFmpegError:
        return audio
    if info.channels < 2:
        return audio
    out = str(Path(audio).with_suffix(".centre.wav"))
    ffmpeg.run(["-i", audio, "-af", "pan=mono|c0=0.5*c0+0.5*c1", out])
    return out


def _speech_islands(spans: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    islands: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        if start - cursor > 0.05:
            islands.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor > 0.05:
        islands.append((cursor, total))
    return [(round(a, 3), round(b, 3)) for a, b in islands if b - a > 0.08]


def _spread_words(text: str, start: float, end: float, speaker: str) -> list[Word]:
    tokens = text.split()
    if not tokens:
        return []
    span = max(end - start, 0.2)
    step = span / len(tokens)
    return [
        Word(
            start=round(start + i * step, 3),
            end=round(start + (i + 1) * step, 3),
            text=tok,
            speaker=speaker,
        )
        for i, tok in enumerate(tokens)
    ]


class StubTranslator(Translator):
    spec = Spec(
        name="stub",
        kind="translate",
        title="Identity 'translator' (offline)",
        languages="none",
        verified=True,
        quality="Does not translate. Copies the source text so the rest of the pipeline can be tested.",
        notes="QC marks every segment it touched, so a stub run can never look like a real dub.",
    )

    def translate(
        self, segments, source, target, glossary=None, instructions=None, context=""
    ) -> None:
        for seg in segments:
            seg.target_text = seg.text
            seg.translator = "stub-identity"
            seg.notes.append(f"untranslated: stub translator, target '{target}' was not applied")


class StubTTS(TTSProvider):
    spec = Spec(
        name="stub",
        kind="tts",
        title="Tone generator (offline)",
        capabilities=("duration_follows_text",),
        languages="none",
        verified=True,
        quality="A tone, not a voice. Right length, right place, no words.",
        notes="Used by `dubforge demo` to prove the timing and mix chain without a key.",
    )

    def voices(self, language: str = "") -> list[Voice]:
        return [
            Voice(id="tone-a", name="Tone A (440 Hz)", gender="n/a"),
            Voice(id="tone-b", name="Tone B (330 Hz)", gender="n/a"),
        ]

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        seconds = max(0.35, estimate_seconds(text, self.settings.target_lang))
        freq = 330.0 if (voice or "").endswith("b") else 440.0
        ffmpeg.write_tone_sequence(out_path, [(0.0, seconds, freq)], seconds, sample_rate)
        return SynthResult(
            path=out_path,
            sample_rate=sample_rate,
            duration=round(seconds, 3),
            voice=voice,
            provider="stub",
            note="tone, not speech",
        )


class NullSeparator(Separator):
    spec = Spec(
        name="none",
        kind="separator",
        title="No separation",
        verified=True,
        quality="Keeps the original audio as the bed, dialogue included. Fine for clean voice-over material.",
    )

    def separate(self, audio: str, out_dir: str) -> tuple[str | None, str | None]:
        return None, None


class NoLipSync(LipSync):
    spec = Spec(
        name="none",
        kind="lipsync",
        title="No lip sync",
        verified=True,
        quality="Audio-only dubbing. The picture is untouched.",
    )

    def sync(self, video: str, audio: str, out_path: str) -> str:
        return video
