"""The provider registry: what exists, what is ready, and what "auto" picks.

`auto` is ordered by quality first, cost second, and it never silently falls back
to a stub — a dub that quietly did nothing is worse than an error that says which
key is missing.
"""

from __future__ import annotations

from ..config import Settings
from .asr import (
    DeepgramASR,
    ElevenLabsScribeASR,
    FasterWhisperASR,
    GroqWhisperASR,
    OpenAIWhisperASR,
    TranscriptFileASR,
)
from .base import (
    ASRProvider,
    LipSync,
    Provider,
    ProviderUnavailable,
    Separator,
    Translator,
    TTSProvider,
)
from .lipsync import NoLipSync, SyncLabsLipSync
from .separate import DemucsSeparator, FFmpegCenterSeparator, NoneSeparator
from .stub import StubASR, StubTranslator, StubTTS
from .translate import (
    DeepLTranslator,
    GeminiTranslator,
    OllamaTranslator,
    OpenAITranslator,
    SarvamTranslator,
)
from .tts import EdgeTTS, ElevenLabsTTS, GoogleTTS, OpenAITTS, SarvamTTS

REGISTRY: dict[str, dict[str, type[Provider]]] = {
    "asr": {
        "faster_whisper": FasterWhisperASR,
        "deepgram": DeepgramASR,
        "elevenlabs": ElevenLabsScribeASR,
        "groq": GroqWhisperASR,
        "openai": OpenAIWhisperASR,
        "transcript": TranscriptFileASR,
        "stub": StubASR,
    },
    "translate": {
        "openai": OpenAITranslator,
        "gemini": GeminiTranslator,
        "sarvam": SarvamTranslator,
        "deepl": DeepLTranslator,
        "ollama": OllamaTranslator,
        "stub": StubTranslator,
    },
    "tts": {
        "elevenlabs": ElevenLabsTTS,
        "sarvam": SarvamTTS,
        "openai": OpenAITTS,
        "google": GoogleTTS,
        "edge_tts": EdgeTTS,
        "stub": StubTTS,
    },
    "separator": {
        "demucs": DemucsSeparator,
        "ffmpeg_center": FFmpegCenterSeparator,
        "none": NoneSeparator,
    },
    "lipsync": {
        "synclabs": SyncLabsLipSync,
        "none": NoLipSync,
    },
}

# What `auto` tries, in order. Quality first, then cost.
AUTO: dict[str, list[str]] = {
    "asr": ["faster_whisper", "deepgram", "elevenlabs", "groq", "openai"],
    "translate": ["openai", "gemini", "sarvam", "deepl", "ollama"],
    "tts": ["elevenlabs", "sarvam", "openai", "google", "edge_tts"],
    "separator": ["demucs", "ffmpeg_center", "none"],
    "lipsync": ["none"],
}

KINDS = tuple(REGISTRY)


def names(kind: str) -> list[str]:
    return list(REGISTRY.get(kind, {}))


def create(kind: str, name: str, settings: Settings | None = None) -> Provider:
    table = REGISTRY.get(kind)
    if table is None:
        raise ProviderUnavailable(f"unknown stage '{kind}'. Known stages: {', '.join(KINDS)}")
    cls = table.get(name)
    if cls is None:
        raise ProviderUnavailable(
            f"unknown {kind} provider '{name}'. Available: {', '.join(sorted(table))}"
        )
    return cls(settings)


def resolve_chain(
    kind: str, settings: Settings | None = None, requested: str | None = None
) -> list[Provider]:
    """Every provider that could run this stage, best first.

    A named provider yields a chain of one. `auto` yields every *ready* provider in
    quality order, which is what makes an opt-in fallback possible without a second
    registry and without changing what `resolve` returns.
    """
    settings = settings or Settings()
    want = (requested or getattr(settings, _field(kind), "auto") or "auto").strip()
    if want and want != "auto":
        provider = create(kind, want, settings)
        provider.require()
        return [provider]

    ready: list[Provider] = []
    tried: list[str] = []
    for name in AUTO.get(kind, []):
        try:
            provider = create(kind, name, settings)
        except ProviderUnavailable:
            continue
        gaps = provider.missing()
        if gaps:
            tried.append(f"{name}: {gaps[0]}")
            continue
        ready.append(provider)
    if ready:
        return ready

    hint = (
        f"no {kind} provider is ready. Tried: "
        + ("; ".join(tried) if tried else "nothing")
        + "\nFix one of them, or name one explicitly with --"
        + kind
        + " <name>."
        + "\n  free/local: "
        + (
            "faster_whisper (pip install faster-whisper)"
            if kind == "asr"
            else "ollama (local model)"
            if kind == "translate"
            else "edge_tts (pip install edge-tts)"
            if kind == "tts"
            else "none"
        )
        + "\nRun `dubforge doctor` for the full picture."
    )
    raise ProviderUnavailable(hint)


def resolve(kind: str, settings: Settings | None = None, requested: str | None = None) -> Provider:
    """`auto` → the first available provider in quality order; otherwise the named one."""
    return resolve_chain(kind, settings, requested)[0]


def _field(kind: str) -> str:
    return {
        "asr": "asr",
        "translate": "translator",
        "tts": "tts",
        "separator": "separator",
        "lipsync": "lipsync",
    }[kind]


def all_specs(settings: Settings | None = None) -> list[dict]:
    """Every adapter, with availability and exactly what is missing. No secrets."""
    settings = settings or Settings()
    out = []
    for kind, table in REGISTRY.items():
        for name, cls in table.items():
            provider = cls(settings)
            row = provider.describe()
            row["auto_rank"] = (
                AUTO.get(kind, []).index(name) if name in AUTO.get(kind, []) else None
            )
            out.append(row)
    return out


def ready_summary(settings: Settings | None = None) -> dict[str, dict]:
    """For each stage: what `auto` would pick right now, and whether it can run."""
    settings = settings or Settings()
    summary = {}
    for kind in KINDS:
        try:
            provider = resolve(kind, settings)
            summary[kind] = {"auto": provider.name, "ready": True, "missing": []}
        except ProviderUnavailable as exc:
            summary[kind] = {"auto": None, "ready": False, "missing": str(exc).splitlines()[:4]}
    return summary


# --------------------------------------------------------------------------- typing


def as_asr(provider: Provider) -> ASRProvider:
    if not isinstance(provider, ASRProvider):
        raise ProviderUnavailable(f"{provider.name} is not an ASR provider")
    return provider


def as_translator(provider: Provider) -> Translator:
    if not isinstance(provider, Translator):
        raise ProviderUnavailable(f"{provider.name} is not a translator")
    return provider


def as_tts(provider: Provider) -> TTSProvider:
    if not isinstance(provider, TTSProvider):
        raise ProviderUnavailable(f"{provider.name} is not a TTS provider")
    return provider


def as_separator(provider: Provider) -> Separator:
    if not isinstance(provider, Separator):
        raise ProviderUnavailable(f"{provider.name} is not a separator")
    return provider


def as_lipsync(provider: Provider) -> LipSync:
    if not isinstance(provider, LipSync):
        raise ProviderUnavailable(f"{provider.name} is not a lip sync provider")
    return provider
