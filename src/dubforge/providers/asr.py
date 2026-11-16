"""Speech recognition adapters: local Whisper, or four hosted recognisers.

All of them return the same `Transcript`, with word timings where the vendor has
them. Word timings are not a luxury here — `timeline.merge_words` uses them to
find where a sentence really ends, and a dub built on 30-second blocks is unusable.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from ..media import Segment, Transcript, Word
from .base import ASRProvider, ProviderError, Spec
from .http import post


def _segments_from_words(words: list[Word]) -> list[Segment]:
    from ..timeline import merge_words

    return merge_words(words)


class FasterWhisperASR(ASRProvider):
    spec = Spec(
        name="faster_whisper",
        kind="asr",
        title="Whisper, running locally (faster-whisper)",
        packages=("faster_whisper",),
        capabilities=("word_timings", "vad", "translate_to_english"),
        languages="99 languages, offline",
        verified=False,
        quality="Strong accuracy, no per-minute cost, needs CPU time. No diarisation: one speaker per run.",
        notes="Model downloads on first use (~150 MB for 'small', ~1.5 GB for 'large-v3'). "
        "Set DUBFORGE_ASR_MODEL to trade speed for accuracy.",
        docs="https://github.com/SYSTRAN/faster-whisper",
    )

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        self.require()
        from faster_whisper import WhisperModel

        size = self.settings.asr_model or "small"
        model = WhisperModel(size, device="auto", compute_type="int8")
        lang = None if (language in (None, "", "auto")) else language
        iterator, info = model.transcribe(
            audio,
            language=lang,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
        )
        words: list[Word] = []
        for chunk in iterator:
            if chunk.words:
                for w in chunk.words:
                    words.append(Word(start=float(w.start), end=float(w.end), text=w.word.strip()))
            else:
                words.append(
                    Word(start=float(chunk.start), end=float(chunk.end), text=chunk.text.strip())
                )
        segments = _segments_from_words(words)
        for seg in segments:
            seg.speaker = "SPEAKER_00"
        notes = ["single speaker assumed: faster-whisper does not diarise"]
        if diarize:
            notes.append(
                "for multi-speaker audio use --asr deepgram, elevenlabs or stub with labels"
            )
        return Transcript(
            segments=segments,
            language=getattr(info, "language", None) or lang or "unknown",
            provider="faster_whisper",
            speakers=["SPEAKER_00"],
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            notes=notes,
        )


class _OpenAICompatibleASR(ASRProvider):
    """Shared shape for the /audio/transcriptions endpoints (OpenAI and Groq)."""

    base_url = ""
    default_model = ""
    provider_name = ""

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        self.require()
        key = self.settings.provider_key(self.spec.name) or ""
        data = {
            "model": self.settings.asr_model or self.default_model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if language and language != "auto":
            data["language"] = language
        with open(audio, "rb") as handle:
            resp = post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data=data,
                files={"file": (Path(audio).name, handle, "audio/wav")},
                timeout=self.settings.timeout_s,
                provider=self.provider_name,
            )
        payload = resp.json()
        words: list[Word] = []
        for w in payload.get("words") or []:
            words.append(
                Word(start=float(w["start"]), end=float(w["end"]), text=str(w["word"]).strip())
            )
        if not words:
            for s in payload.get("segments") or []:
                words.append(
                    Word(
                        start=float(s["start"]),
                        end=float(s["end"]),
                        text=str(s.get("text", "")).strip(),
                    )
                )
        segments = _segments_from_words(words)
        notes = []
        if diarize:
            notes.append("no diarisation on this endpoint: every segment is SPEAKER_00")
        return Transcript(
            segments=segments,
            language=payload.get("language") or language or "unknown",
            provider=self.spec.name,
            speakers=["SPEAKER_00"],
            duration=float(payload.get("duration") or 0.0),
            notes=notes,
        )


class OpenAIWhisperASR(_OpenAICompatibleASR):
    base_url = "https://api.openai.com/v1"
    default_model = "whisper-1"
    provider_name = "openai"
    spec = Spec(
        name="openai",
        kind="asr",
        title="OpenAI Whisper API",
        env=("OPENAI_API_KEY",),
        capabilities=("word_timings",),
        languages="99 languages",
        verified=False,
        quality="Good accuracy, simple to run, no speaker labels.",
        notes="~$0.006 per audio minute (list price). DUBFORGE_ASR_MODEL=gpt-4o-transcribe is cheaper/faster "
        "but its timestamp granularity is coarser — measure on your own footage.",
        docs="https://platform.openai.com/docs/guides/speech-to-text",
    )


class GroqWhisperASR(_OpenAICompatibleASR):
    base_url = "https://api.groq.com/openai/v1"
    default_model = "whisper-large-v3-turbo"
    provider_name = "groq"
    spec = Spec(
        name="groq",
        kind="asr",
        title="Groq Whisper (fastest, cheapest per minute)",
        env=("GROQ_API_KEY",),
        capabilities=("word_timings",),
        languages="99 languages",
        verified=False,
        quality="Whisper large-v3-turbo at very high speed; excellent for long footage on a budget.",
        notes="~$0.0007 per audio minute at list price — roughly 6x cheaper than OpenAI Whisper.",
        docs="https://console.groq.com/docs/speech-to-text",
    )


class DeepgramASR(ASRProvider):
    spec = Spec(
        name="deepgram",
        kind="asr",
        title="Deepgram Nova-3 with diarisation",
        env=("DEEPGRAM_API_KEY",),
        capabilities=("diarize", "word_timings", "utterances", "keyterms"),
        languages="30+ languages",
        verified=False,
        quality="Best-in-class speaker diarisation and word timings — the default choice for interviews and drama.",
        notes="Diarisation is what keeps two actors from sharing one voice. ~$0.0043 per minute.",
        docs="https://developers.deepgram.com/docs/diarization",
    )

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        self.require()
        key = self.settings.provider_key("deepgram") or ""
        params = {
            "model": self.settings.asr_model or "nova-3",
            "smart_format": "true",
            "utterances": "true",
            "diarize": "true" if diarize else "false",
            "punctuate": "true",
        }
        if language and language != "auto":
            params["language"] = language
        query = urlencode(params)
        with open(audio, "rb") as handle:
            resp = post(
                f"https://api.deepgram.com/v1/listen?{query}",
                headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
                data=handle.read(),
                timeout=self.settings.timeout_s,
                provider="deepgram",
            )
        payload = resp.json()
        results = payload.get("results") or {}
        channel = (results.get("channels") or [{}])[0] or {}
        alt = (channel.get("alternatives") or [{}])[0] or {}
        words: list[Word] = []
        for w in alt.get("words") or []:
            words.append(
                Word(
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                    text=str(w.get("punctuated_word") or w.get("word") or "").strip(),
                    speaker=f"SPEAKER_{int(w.get('speaker', 0)):02d}" if diarize else None,
                    confidence=float(w.get("confidence", 0.0)) or None,
                )
            )
        utterances = results.get("utterances") or []
        if utterances:
            segments = [
                Segment(
                    index=i,
                    start=float(u.get("start", 0.0)),
                    end=float(u.get("end", 0.0)),
                    text=str(u.get("transcript", "")).strip(),
                    speaker=f"SPEAKER_{int(u.get('speaker', 0)):02d}",
                )
                for i, u in enumerate(utterances)
            ]
            # keep word timings on the segments that contain them
            for seg in segments:
                seg.words = [w for w in words if seg.start - 0.01 <= w.start <= seg.end + 0.01]
        else:
            segments = _segments_from_words(words)
        speakers = sorted({s.speaker for s in segments}) or ["SPEAKER_00"]
        return Transcript(
            segments=segments,
            language=params.get("language", language or "unknown"),
            provider="deepgram",
            speakers=speakers,
            duration=float((payload.get("metadata") or {}).get("duration") or 0.0),
        )


class ElevenLabsScribeASR(ASRProvider):
    spec = Spec(
        name="elevenlabs",
        kind="asr",
        title="ElevenLabs Scribe v2",
        env=("ELEVENLABS_API_KEY",),
        capabilities=("diarize", "word_timings", "audio_events"),
        languages="90+ languages",
        verified=False,
        quality="Very strong on accents, names and messy audio; also flags laughter and music, which helps QC.",
        notes="~$0.0037 per minute. Pairing Scribe with ElevenLabs voices keeps one vendor's idea of the speaker.",
        docs="https://elevenlabs.io/docs/api-reference/speech-to-text",
    )

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        self.require()
        key = self.settings.provider_key("elevenlabs") or ""
        data = {
            "model_id": self.settings.asr_model or "scribe_v2",
            "diarize": "true" if diarize else "false",
            "timestamps_granularity": "word",
        }
        if language and language != "auto":
            data["language_code"] = language
        with open(audio, "rb") as handle:
            resp = post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": key},
                data=data,
                files={"file": (Path(audio).name, handle, "audio/wav")},
                timeout=self.settings.timeout_s,
                provider="elevenlabs",
            )
        payload = resp.json()
        words: list[Word] = []
        for w in payload.get("words") or []:
            if str(w.get("type", "word")) != "word":
                continue
            speaker = w.get("speaker_id")
            words.append(
                Word(
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                    text=str(w.get("text", "")).strip(),
                    speaker=f"SPEAKER_{int(speaker):02d}" if isinstance(speaker, int) else None,
                )
            )
        segments = _segments_from_words(words)
        return Transcript(
            segments=segments,
            language=payload.get("language_code") or language or "unknown",
            provider="elevenlabs",
            speakers=sorted({s.speaker for s in segments}) or ["SPEAKER_00"],
            duration=float(payload.get("audio_duration_seconds") or 0.0),
            notes=[] if diarize else ["diarisation disabled"],
        )


class TranscriptFileASR(ASRProvider):
    """Use a transcript you already have (a .json from a previous run, or a .srt)."""

    spec = Spec(
        name="transcript",
        kind="asr",
        title="Reuse a transcript (JSON or SRT)",
        capabilities=("diarize",),
        languages="n/a",
        verified=True,
        quality="Exactly as good as the transcript you supply — and free.",
        notes="Point DUBFORGE_TRANSCRIPT at the file. JSON must be a dubforge transcript dump.",
    )

    def transcribe(
        self, audio: str, language: str | None = None, diarize: bool = True
    ) -> Transcript:
        path = self.settings.transcript
        if not path:
            raise ProviderError("set --transcript <file.json|file.srt> to reuse a transcript")
        if str(path).lower().endswith(".srt"):
            from ..srt import read_srt

            segments = read_srt(path)
            return Transcript(
                segments=segments,
                language=language or "unknown",
                provider="transcript-srt",
                speakers=["SPEAKER_00"],
            )
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "segments" not in data:
            raise ProviderError(f"{path} is not a dubforge transcript (no 'segments' key)")
        return Transcript.from_dict(data)
