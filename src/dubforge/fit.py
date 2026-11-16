"""Duration-aware translation: the part that makes a dub sound dubbed.

A translator that ignores the clock produces lines that are correct and unusable:
the sentence runs 40% longer than the actor's mouth, so either it gets cut, or it
gets sped up until it sounds like a chipmunk, or it pushes every later line out of
sync. This module turns "how long does this take to say" into a number before a
single character is synthesised, so the translator can be asked for a *shorter*
rendering instead of the synthesiser being tortured afterwards.

The model is deliberately simple and documented: characters per second of natural
speech, per language. It is a planning estimate, not a measurement — the measured
duration comes back from the TTS provider and is what QC reports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .media import Segment

# Characters per second of comfortable narration, by language. Derived from the
# usual ~150 wpm English narration rate and adjusted for script density
# (Devanagari, Arabic and Tamil carry more meaning per character than Latin;
# Japanese and Chinese fewer characters per second but far more per character).
# Override per project with `speech_cps` in the config if a voice reads faster.
CPS: dict[str, float] = {
    "en": 15.0,
    "hi": 13.5,
    "mr": 13.5,
    "ne": 13.0,
    "bn": 12.5,
    "gu": 13.0,
    "pa": 13.0,
    "or": 12.5,
    "as": 12.5,
    "ta": 11.5,
    "te": 11.5,
    "kn": 11.5,
    "ml": 11.5,
    "ur": 12.5,
    "es": 15.5,
    "pt": 15.0,
    "it": 15.0,
    "fr": 14.5,
    "de": 13.5,
    "nl": 14.0,
    "ru": 13.0,
    "tr": 13.5,
    "id": 14.5,
    "vi": 13.0,
    "th": 12.0,
    "ar": 12.5,
    "fa": 12.5,
    "ko": 10.5,
    "ja": 8.5,
    "zh": 7.0,
}
DEFAULT_CPS = 13.0


def cps_for(language: str) -> float:
    return CPS.get((language or "").lower().split("-")[0], DEFAULT_CPS)


def estimate_seconds(text: str, language: str) -> float:
    """Rough spoken duration of `text` in `language`."""
    chars = len(" ".join((text or "").split()))
    if not chars:
        return 0.0
    return chars / cps_for(language)


@dataclass
class Fit:
    """What a segment needs in order to fit its window."""

    window: float
    estimate: float
    ratio: float
    verdict: str  # "ok" | "stretch" | "rewrite"
    target_chars: int

    def to_dict(self) -> dict:
        return {
            "window": round(self.window, 3),
            "estimate": round(self.estimate, 3),
            "ratio": round(self.ratio, 3),
            "verdict": self.verdict,
            "target_chars": self.target_chars,
        }


def target_chars(window: float, language: str, max_speed: float = 1.0) -> int:
    """How many characters fit in `window` at the fastest acceptable speaking rate."""
    return max(0, int(window * cps_for(language) * max(1.0, max_speed)))


def fit(
    text: str,
    window: float,
    language: str,
    max_speed: float = 1.15,
    min_speed: float = 0.9,
) -> Fit:
    """Classify a candidate translation against the time it has to occupy.

    ``max_speed`` is the fastest we are willing to play the voice back (atempo,
    pitch preserved). Beyond it, asking for a shorter sentence sounds better than
    compressing a long one.
    """
    window = max(0.25, window)
    est = estimate_seconds(text, language)
    ratio = est / window if window else 99.0
    if ratio <= 1.0:
        verdict = "ok" if ratio >= min_speed else "ok"  # short lines get a pause, not a stretch
    elif ratio <= max_speed:
        verdict = "stretch"
    else:
        verdict = "rewrite"
    return Fit(
        window=window,
        estimate=est,
        ratio=ratio,
        verdict=verdict,
        target_chars=target_chars(window, language, max_speed),
    )


def plan(segments: list[Segment], language: str, max_speed: float = 1.15) -> list[Fit]:
    """Fit verdict for every segment, using the window already written onto it."""
    return [fit(s.speech, s.window or s.duration, language, max_speed) for s in segments]


def needs_rewrite(segments: list[Segment], language: str, max_speed: float = 1.15) -> list[Segment]:
    return [
        s
        for s, f in zip(segments, plan(segments, language, max_speed), strict=False)
        if f.verdict == "rewrite"
    ]


def constraint_note(seg: Segment, language: str, max_speed: float = 1.15) -> str:
    """The instruction handed back to the translator for one over-long line."""
    limit = target_chars(seg.window or seg.duration, language, max_speed)
    have = len(" ".join(seg.speech.split()))
    return (
        f"This line must be spoken in {seg.window or seg.duration:.1f}s "
        f"(about {limit} characters in {language}). The current translation is "
        f"{have} characters, roughly {estimate_seconds(seg.speech, language):.1f}s. "
        f"Rewrite it shorter: keep the meaning and the speaker's intent, drop filler "
        f"and repetition, prefer shorter synonyms. Do not drop a fact the line exists to carry."
    )


def total_speech_seconds(segments: list[Segment], language: str) -> float:
    return sum(estimate_seconds(s.speech, language) for s in segments)
