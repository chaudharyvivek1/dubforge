"""Text-to-speech adapters.

Two things decide whether a dub sounds dubbed rather than read out: the voice
stays the same person for the whole film, and each line lands in the space it has.
The first is a mapping problem (`timeline.map_voices`); the second is measured
after synthesis, in the pipeline. These adapters just have to render clean audio
and tell the truth about what they rendered.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from .. import ffmpeg
from .base import ProviderError, Spec, SynthResult, TTSProvider, Voice
from .http import get, post

# "hi-IN" style locale codes, per provider, for the languages this project targets.
LOCALE = {
    "hi": "hi-IN",
    "bn": "bn-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "pa": "pa-IN",
    "or": "or-IN",
    "as": "as-IN",
    "ur": "ur-PK",
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "pt": "pt-BR",
    "it": "it-IT",
    "ar": "ar-SA",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "ru": "ru-RU",
    "id": "id-ID",
    "tr": "tr-TR",
    "vi": "vi-VN",
    "th": "th-TH",
    "nl": "nl-NL",
}


def locale_for(language: str, default: str = "en-US") -> str:
    return LOCALE.get((language or "").lower().split("-")[0], default)


def _as_wav(src: str, dst: str, sample_rate: int = 48000) -> str:
    """Vendors return mp3/opus/raw; the mixer wants one predictable format."""
    if src == dst and dst.endswith(".wav"):
        return dst
    ffmpeg.extract_audio(src, dst, sample_rate=sample_rate, channels=1)
    return dst


def _duration(path: str) -> float | None:
    try:
        return round(ffmpeg.duration_of(path), 3)
    except Exception:  # noqa: BLE001 - a duration we cannot measure is not fatal
        return None


class ElevenLabsTTS(TTSProvider):
    spec = Spec(
        name="elevenlabs",
        kind="tts",
        title="ElevenLabs multilingual v2 (voice cloning)",
        env=("ELEVENLABS_API_KEY",),
        capabilities=("voice_clone", "style", "emotion", "multilingual", "voice_library"),
        languages="29 languages on multilingual v2; 70+ on v3 models",
        verified=False,
        quality="The reference for emotion and identity: a cloned voice keeps its timbre across languages. "
        "~$0.08 per 1,000 characters on multilingual v2.",
        notes="Clone the original actor's voice only with that person's written permission — see docs/security.md. "
        "Use --tts elevenlabs --voice-pool <id,id> or a voice map per speaker.",
        docs="https://elevenlabs.io/docs/api-reference/text-to-speech",
    )

    base = "https://api.elevenlabs.io/v1"

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.settings.provider_key("elevenlabs") or ""}

    def voices(self, language: str = "") -> list[Voice]:
        if not self.available:
            return []
        resp = get(f"{self.base}/voices", headers=self._headers(), provider="elevenlabs")
        out = []
        for row in resp.json().get("voices", []):
            labels = row.get("labels") or {}
            out.append(
                Voice(
                    id=row.get("voice_id", ""),
                    name=row.get("name", ""),
                    language=(row.get("fine_tuning") or {}).get("language", "") or "",
                    gender=labels.get("gender", ""),
                    style=", ".join(str(v) for v in labels.values() if v)[:80],
                    preview=row.get("preview_url", ""),
                )
            )
        return out

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        self.require()
        if not voice:
            raise ProviderError("elevenlabs needs a voice id (use --voice-pool or --voice-map)")
        model = self.settings.tts_model or "eleven_multilingual_v2"
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.35 if (style or emotion) else 0.15,
                "use_speaker_boost": True,
            },
        }
        if self.settings.target_lang:
            body["language_code"] = (self.settings.target_lang or "").split("-")[0]
        raw = str(Path(out_path).with_suffix(".mp3"))
        resp = post(
            f"{self.base}/text-to-speech/{voice}?output_format=mp3_44100_128",
            headers={**self._headers(), "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json_body=body,
            timeout=self.settings.timeout_s,
            provider="elevenlabs",
        )
        Path(raw).write_bytes(resp.content)
        wav = _as_wav(raw, str(out_path), sample_rate)
        Path(raw).unlink(missing_ok=True)
        return SynthResult(
            path=wav,
            sample_rate=sample_rate,
            duration=_duration(wav),
            voice=voice,
            provider="elevenlabs",
            note=model,
        )


class OpenAITTS(TTSProvider):
    spec = Spec(
        name="openai",
        kind="tts",
        title="OpenAI speech (gpt-4o-mini-tts / tts-1)",
        env=("OPENAI_API_KEY",),
        capabilities=("style_instructions", "cheap", "many_voices"),
        languages="50+ languages",
        verified=False,
        quality="Clean and steady; style can be steered with a sentence of instructions. No cloning.",
        notes="tts-1 is ~$15 per 1M characters. gpt-4o-mini-tts accepts an `instructions` field — that is "
        "where --style ends up.",
        docs="https://platform.openai.com/docs/guides/text-to-speech",
    )

    VOICES = (
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    )

    def voices(self, language: str = "") -> list[Voice]:
        return [Voice(id=v, name=v.title()) for v in self.VOICES]

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        self.require()
        model = self.settings.tts_model or "gpt-4o-mini-tts"
        body: dict = {
            "model": model,
            "voice": voice or "alloy",
            "input": text,
            "response_format": "wav",
        }
        hint = " ".join(x for x in (style, emotion) if x)
        if hint and model.startswith("gpt-4o"):
            body["instructions"] = (
                f"Speak as a dubbing actor would: {hint}. Keep it natural and unhurried."
            )
        raw = str(Path(out_path).with_suffix(".raw.wav"))
        resp = post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {self.settings.provider_key('openai') or ''}",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=self.settings.timeout_s,
            provider="openai",
        )
        Path(raw).write_bytes(resp.content)
        wav = _as_wav(raw, str(out_path), sample_rate)
        Path(raw).unlink(missing_ok=True)
        return SynthResult(
            path=wav,
            sample_rate=sample_rate,
            duration=_duration(wav),
            voice=voice or "alloy",
            provider="openai",
            note=model,
        )


class GoogleTTS(TTSProvider):
    spec = Spec(
        name="google",
        kind="tts",
        title="Google Cloud Text-to-Speech",
        env=("GOOGLE_API_KEY",),
        capabilities=("ssml", "many_locales", "studio_voices"),
        languages="50+ languages, per-locale voices",
        verified=False,
        quality="Reliable and cheap for Indic locales; the Studio voices are noticeably better than Standard.",
        notes="Neural2 ~$16 per 1M characters. Pass a full voice name in --voice-pool, e.g. hi-IN-Neural2-A.",
        docs="https://cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize",
    )

    def voices(self, language: str = "") -> list[Voice]:
        return []

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        self.require()
        lang = locale_for(self.settings.target_lang)
        body = {
            "input": {"text": text},
            "voice": {"languageCode": lang, **({"name": voice} if voice else {})},
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": sample_rate,
                "speakingRate": 1.0,
            },
        }
        resp = post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            headers={
                "x-goog-api-key": self.settings.provider_key("google") or "",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=self.settings.timeout_s,
            provider="google",
        )
        payload = resp.json()
        audio_b64 = payload.get("audioContent")
        if not audio_b64:
            raise ProviderError(f"google returned no audio: {str(payload)[:200]}")
        Path(out_path).write_bytes(base64.b64decode(audio_b64))
        return SynthResult(
            path=str(out_path),
            sample_rate=sample_rate,
            duration=_duration(str(out_path)),
            voice=voice or lang,
            provider="google",
        )


class SarvamTTS(TTSProvider):
    spec = Spec(
        name="sarvam",
        kind="tts",
        title="Sarvam Bulbul (Indic voices)",
        env=("SARVAM_API_KEY",),
        capabilities=("indic", "natural_prosody"),
        languages="11 Indian languages",
        verified=False,
        quality="The most natural Indian-language voices available without cloning; handles Devanagari, "
        "Tamil, Telugu and Bengali script correctly, which many global engines do not.",
        notes="Speakers include anushka, abhilash, manisha, vidya, arya, karun, hitesh. "
        "Set --voice-pool anushka,abhilash for two-speaker material.",
        docs="https://docs.sarvam.ai/api-reference-docs/text-to-speech",
    )

    SPEAKERS = ("anushka", "abhilash", "manisha", "vidya", "arya", "karun", "hitesh")

    def voices(self, language: str = "") -> list[Voice]:
        return [Voice(id=s, name=s.title(), language=language or "hi") for s in self.SPEAKERS]

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        self.require()
        body = {
            "text": text,
            "target_language_code": locale_for(self.settings.target_lang),
            "speaker": voice or "anushka",
            "model": self.settings.tts_model or "bulbul:v2",
            "speech_sample_rate": sample_rate,
            "enable_preprocessing": True,
        }
        resp = post(
            "https://api.sarvam.ai/text-to-speech",
            headers={
                "api-subscription-key": self.settings.provider_key("sarvam") or "",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=self.settings.timeout_s,
            provider="sarvam",
        )
        payload = resp.json()
        audios = payload.get("audios") or []
        if not audios:
            raise ProviderError(f"sarvam returned no audio: {str(payload)[:200]}")
        Path(out_path).write_bytes(base64.b64decode(audios[0]))
        return SynthResult(
            path=str(out_path),
            sample_rate=sample_rate,
            duration=_duration(str(out_path)),
            voice=voice or "anushka",
            provider="sarvam",
        )


class EdgeTTS(TTSProvider):
    spec = Spec(
        name="edge_tts",
        kind="tts",
        title="edge-tts (free Microsoft neural voices)",
        env=(),
        packages=("edge_tts",),
        capabilities=("free", "many_locales", "no_key"),
        languages="100+ locales",
        verified=False,
        quality="Surprisingly good neural voices for zero cost, and there are real Indic voices "
        "(hi-IN-SwaraNeural, ta-IN-PallaviNeural…). No cloning, no emotion control, no SLA.",
        notes="An unofficial endpoint. Perfect for a school lab or a first pass; read the terms before "
        "commercial use. pip install edge-tts",
        docs="https://github.com/rany2/edge-tts",
    )

    def voices(self, language: str = "") -> list[Voice]:
        try:
            import asyncio

            import edge_tts

            rows = asyncio.run(edge_tts.list_voices())
        except Exception:  # noqa: BLE001 - offline is a normal state here
            return []
        prefix = (language or "").split("-")[0].lower()
        out = []
        for row in rows:
            if prefix and not str(row.get("Locale", "")).lower().startswith(prefix):
                continue
            out.append(
                Voice(
                    id=row.get("ShortName", ""),
                    name=row.get("FriendlyName", ""),
                    language=row.get("Locale", ""),
                    gender=row.get("Gender", ""),
                )
            )
        return out

    def synthesize(
        self, text, voice, out_path, style=None, emotion=None, sample_rate=48000
    ) -> SynthResult:
        self.require()
        import asyncio

        import edge_tts

        chosen = voice or f"{locale_for(self.settings.target_lang)}-Neural"
        raw = str(Path(out_path).with_suffix(".mp3"))
        rate = self.settings.tts_rate or "+0%"
        communicate = edge_tts.Communicate(text, chosen, rate=rate)
        asyncio.run(communicate.save(raw))
        wav = _as_wav(raw, str(out_path), sample_rate)
        Path(raw).unlink(missing_ok=True)
        return SynthResult(
            path=wav,
            sample_rate=sample_rate,
            duration=_duration(wav),
            voice=chosen,
            provider="edge_tts",
            note="unofficial endpoint",
        )


def voices_json(voices: list[Voice]) -> str:
    return json.dumps([v.to_dict() for v in voices], ensure_ascii=False)
