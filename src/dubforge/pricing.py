"""Indicative list prices, so `dubforge plan` can answer "what will this cost?".

Read from vendor pricing pages in November 2026 and quoted in USD. They are list
prices for pay-as-you-go use, before taxes and volume deals, and they change —
treat every number here as an estimate to be re-checked before a budget is signed
off, not a quote. `docs/cost.md` carries the source for each row.

Units are the vendor's own billing unit, converted to a per-minute or
per-million-character figure so different providers can be compared.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED = "2026-11"


@dataclass
class Price:
    kind: str  # "asr" | "translate" | "tts" | "dub" | "lipsync" | "separate"
    provider: str
    model: str
    usd_per_minute: float | None = None  # audio in
    usd_per_1m_chars: float | None = None  # text out (TTS or translation)
    usd_per_1m_tokens: float | None = None  # translation, LLM billed
    note: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


PRICES: list[Price] = [
    # --- speech to text -----------------------------------------------------
    Price(
        "asr",
        "groq",
        "whisper-large-v3-turbo",
        usd_per_minute=0.00067,
        note="$0.04/hour",
        source="groq.com/pricing",
    ),
    Price(
        "asr",
        "openai",
        "gpt-4o-mini-transcribe",
        usd_per_minute=0.003,
        note="vendor's own per-minute estimate",
        source="openai.com/api/pricing",
    ),
    Price(
        "asr",
        "elevenlabs",
        "scribe-v2",
        usd_per_minute=0.0037,
        note="$0.22/hour",
        source="elevenlabs.io/pricing",
    ),
    Price(
        "asr",
        "deepgram",
        "nova-3-prerecorded",
        usd_per_minute=0.0043,
        note="diarisation included",
        source="deepgram.com/pricing",
    ),
    Price("asr", "openai", "whisper-1", usd_per_minute=0.006, source="openai.com/api/pricing"),
    Price(
        "asr",
        "faster_whisper",
        "local",
        usd_per_minute=0.0,
        note="your hardware and electricity",
        source="local",
    ),
    # --- text to speech -----------------------------------------------------
    Price(
        "tts",
        "elevenlabs",
        "multilingual-v2",
        usd_per_1m_chars=80.0,
        note="$0.08 per 1,000 characters",
        source="elevenlabs.io/pricing",
    ),
    Price(
        "tts",
        "elevenlabs",
        "flash-v2-5",
        usd_per_1m_chars=40.0,
        note="$0.04 per 1,000 characters; cheaper, less expressive",
        source="elevenlabs.io/pricing",
    ),
    Price("tts", "openai", "tts-1", usd_per_1m_chars=15.0, source="openai.com/api/pricing"),
    Price("tts", "deepgram", "aura-2", usd_per_1m_chars=30.0, source="deepgram.com/pricing"),
    Price(
        "tts",
        "google",
        "neural2",
        usd_per_1m_chars=16.0,
        note="standard voices are free under the monthly allowance",
        source="cloud.google.com/text-to-speech/pricing",
    ),
    Price(
        "tts",
        "edge_tts",
        "free-endpoint",
        usd_per_1m_chars=0.0,
        note="unofficial endpoint; no SLA, check the terms before commercial use",
        source="local",
    ),
    # --- end-to-end dubbing (for comparison, not used by the pipeline) ------
    Price(
        "dub",
        "elevenlabs",
        "dubbing-v1",
        usd_per_minute=0.50,
        note="without watermark; $0.33 with",
        source="elevenlabs.io/dubbing-api",
    ),
    Price(
        "dub",
        "elevenlabs",
        "dubbing-v2",
        usd_per_minute=2.20,
        note="voice and emotion preserved, 90+ languages",
        source="elevenlabs.io/dubbing-api",
    ),
    Price(
        "dub",
        "azure",
        "video-translation",
        usd_per_minute=0.33,
        note="$5/hour input + $15/hour output, standard voice",
        source="azure.microsoft.com/pricing",
    ),
    # --- extras -------------------------------------------------------------
    Price(
        "lipsync",
        "synclabs",
        "sync-2",
        usd_per_minute=None,
        note="quote-based; check the dashboard before a large batch",
        source="sync.so/pricing",
    ),
    Price(
        "separate", "demucs", "htdemucs", usd_per_minute=0.0, note="local GPU/CPU", source="local"
    ),
]


def table() -> list[dict]:
    return [p.to_dict() for p in PRICES]


def for_provider(provider: str, kind: str | None = None) -> list[Price]:
    return [p for p in PRICES if p.provider == provider and (kind is None or p.kind == kind)]


def cheapest(kind: str, exclude: tuple[str, ...] = ()) -> Price | None:
    rows = [
        p
        for p in PRICES
        if p.kind == kind
        and p.provider not in exclude
        and (p.usd_per_minute is not None or p.usd_per_1m_chars is not None)
    ]
    if not rows:
        return None
    return min(
        rows,
        key=lambda p: (
            (p.usd_per_minute if p.usd_per_minute is not None else 0)
            + (p.usd_per_1m_chars or 0) / 1000
        ),
    )


def estimate(
    kind: str,
    provider: str,
    minutes: float = 0.0,
    chars: float = 0.0,
    model: str | None = None,
) -> float | None:
    """USD estimate for one stage, or None when the provider publishes no list price."""
    rows = for_provider(provider, kind)
    if model:
        rows = [r for r in rows if r.model == model] or rows
    if not rows:
        return None
    row = rows[0]
    total = 0.0
    if row.usd_per_minute is not None:
        total += row.usd_per_minute * minutes
    if row.usd_per_1m_chars is not None:
        total += row.usd_per_1m_chars * chars / 1_000_000
    return round(total, 4) if total or row.usd_per_minute == 0 else None
