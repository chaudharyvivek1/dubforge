"""Target-language word alignment: measured, or honestly absent.

The rule this module exists to enforce is narrow and easy to break by accident:

> a timestamp is either **measured** by a provider or **not there**. There is no third
> state, and nothing in this package may invent one.

That means no even distribution of a sentence's duration across its words, no interpolating
between the first and last word, no "approximate" flag that a later reader mistakes for
precision. If a TTS provider returns word timings (ElevenLabs' timestamped endpoint does),
they are stored on the segment with `alignment="measured"` and validated. If it does not,
the segment keeps `alignment="none"` and says so in the run's report, and the pipeline stays
on sentence-level timing — which is what it has always used and what it is honest about.

Why it matters: sentence-level timing is enough to place a line, and it is what makes a dub
watchable. Word-level timing is what you need to re-time a single word, or to burn in karaoke
subtitles, or to judge whether one clause drifted. Those are different promises, and a
reviewer must be able to tell which one they were given.
"""

from __future__ import annotations

from .media import Segment, Word

MEASURED = "measured"
NONE = "none"


def validate(words: list[Word], segment: Segment, tolerance: float = 0.25) -> list[str]:
    """Problems with a set of target-word timings. Empty list means usable.

    Checks ordering, positive duration, containment in the segment (with a small tolerance
    for provider rounding) and non-empty text. A provider that returns garbage is a reason to
    fall back to sentence-level timing, not a reason to ship the garbage.
    """
    problems: list[str] = []
    if not words:
        return ["no words"]

    previous_end = None
    for index, word in enumerate(words):
        if not (word.text or "").strip():
            problems.append(f"word {index} has no text")
        if word.end <= word.start:
            problems.append(f"word {index} ({word.text!r}) has a non-positive duration")
        if previous_end is not None and word.start < previous_end - 1e-6:
            problems.append(f"word {index} ({word.text!r}) starts before the previous word ends")
        previous_end = word.end

    start, end = words[0].start, words[-1].end
    if start < segment.start - tolerance:
        problems.append(
            f"alignment starts at {start:.3f}s, before the segment ({segment.start:.3f}s)"
        )
    if end > segment.end + max(tolerance, segment.duration * 0.5):
        problems.append(f"alignment ends at {end:.3f}s, after the segment ({segment.end:.3f}s)")
    return problems


def attach_measured(
    segment: Segment, words: list[Word], provider: str, tolerance: float = 0.25
) -> bool:
    """Store provider-reported word timings, if they survive validation.

    Returns True when the segment now carries measured alignment. On failure the segment is
    left on sentence-level timing with a note saying why — a bad alignment must not degrade a
    good dub.
    """
    problems = validate(words, segment, tolerance)
    if problems:
        segment.alignment = NONE
        segment.target_words = []
        segment.alignment_note = (
            f"{provider} returned word timings that failed validation "
            f"({problems[0]}); sentence-level timing used instead"
        )
        return False
    segment.target_words = list(words)
    segment.alignment = MEASURED
    segment.alignment_note = f"{provider} reported {len(words)} word timings"
    return True


def describe(segment: Segment) -> dict:
    """What a reviewer needs to know about this line's timing precision."""
    return {
        "alignment": segment.alignment,
        "words": len(segment.target_words),
        "note": segment.alignment_note,
        "sentence_level_only": segment.alignment != MEASURED,
    }


def coverage(segments: list[Segment]) -> dict:
    """How much of the dub has measured word alignment, for the run report."""
    measured = [seg for seg in segments if seg.alignment == MEASURED]
    return {
        "segments": len(segments),
        "measured": len(measured),
        "ratio": round(len(measured) / len(segments), 3) if segments else 0.0,
        "sentence_level_only": len(segments) - len(measured),
    }
