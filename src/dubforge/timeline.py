"""Turning ASR output into dub-ready segments, and deciding how much room each one has.

Three jobs, all deterministic and unit-tested:

* `merge_words` — words into sentences a person would actually say in one breath.
* `assign_speakers` — diarisation turns onto those sentences.
* `windows` — for each line, how many seconds the dub may occupy before it would
  step on the next line. This is the number the translator is held to.
"""

from __future__ import annotations

from .media import Segment, Word

SENTENCE_END = tuple(".!?。！？…")
CLAUSE_END = tuple(",;:，、؛")


def merge_words(
    words: list[Word],
    gap: float = 0.7,
    max_duration: float = 12.0,
    max_chars: int = 240,
    hard_stop_on_punctuation: bool = True,
) -> list[Segment]:
    """Group word timings into utterances.

    Breaks on a silence longer than `gap`, on sentence punctuation when the line is
    already long enough to be a sentence, and always before `max_duration` so no
    single TTS request is unbounded.
    """
    segments: list[Segment] = []
    current: list[Word] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = " ".join(w.text.strip() for w in current if w.text.strip()).strip()
        if text:
            speakers = [w.speaker for w in current if w.speaker]
            segments.append(
                Segment(
                    index=len(segments),
                    start=round(current[0].start, 3),
                    end=round(current[-1].end, 3),
                    text=text,
                    speaker=max(set(speakers), key=speakers.count) if speakers else "SPEAKER_00",
                    words=list(current),
                )
            )
        current = []

    for word in words:
        if current:
            prev = current[-1]
            silence = word.start - prev.end
            text_so_far = " ".join(w.text.strip() for w in current)
            long_enough = len(text_so_far) >= max(24, max_chars // 3)
            ends_sentence = hard_stop_on_punctuation and prev.text.strip().endswith(SENTENCE_END)
            over_time = (word.end - current[0].start) > max_duration
            over_chars = len(text_so_far) >= max_chars
            speaker_changed = bool(word.speaker and prev.speaker and word.speaker != prev.speaker)
            if (
                silence > gap
                or over_time
                or over_chars
                or speaker_changed
                or (ends_sentence and long_enough)
            ):
                flush()
        current.append(word)
    flush()

    for i, seg in enumerate(segments):
        seg.index = i
    return segments


def assign_speakers(segments: list[Segment], turns: list[tuple[float, float, str]]) -> None:
    """Label each segment with the speaker who overlaps it most (diarisation turns)."""
    if not turns:
        return
    for seg in segments:
        best, best_overlap = None, 0.0
        for start, end, speaker in turns:
            overlap = min(seg.end, end) - max(seg.start, start)
            if overlap > best_overlap:
                best, best_overlap = speaker, overlap
        if best:
            seg.speaker = best


def speakers_in(segments: list[Segment]) -> list[str]:
    seen: list[str] = []
    for seg in segments:
        if seg.speaker not in seen:
            seen.append(seg.speaker)
    return seen


def windows(
    segments: list[Segment],
    total_duration: float,
    tail_gap: float = 0.12,
    max_stretch: float | None = None,
) -> list[float]:
    """Seconds each dub line may occupy, then written onto `segment.window`.

    The rules, in the order they apply (0.3.0):

    1. **Overlapping source dialogue** (`next.start < seg.end`, two people talking over each
       other): the window is the segment's own duration. Serialising an overlap would mean
       cutting one speaker off to let the other start, which is worse than the overlap.
    2. **Consecutive sentences with a gap**: the window is everything up to `tail_gap` before
       the next line starts, so a dub line may use the silence that follows it.
    3. **An ASR end that runs past the next start** (bad end timestamps): treated as an
       overlap, and `overlaps_next` is set so a reviewer can see the transcription is
       suspect rather than the dub.
    4. **Very short segments**: the window is the room available, *never more*. A floor that
       exceeds the room is how a dub line ends up talking over the next one — the old
       `max(room, …)` did exactly that. `window_tight` records the squeeze instead.
    5. **The last line**: everything left in the runtime, measured from the line's own start.

    `max_stretch` is accepted for compatibility and ignored: it existed only to expand the
    floor, which is the behaviour this version removes.
    """
    ordered = sorted(segments, key=lambda s: s.start)
    out: list[float] = []
    for index, seg in enumerate(ordered):
        next_start = ordered[index + 1].start if index + 1 < len(ordered) else None
        overlaps = next_start is not None and next_start < seg.end
        seg.overlaps_next = bool(overlaps)

        if next_start is not None and not overlaps:
            room = next_start - tail_gap - seg.start
        elif overlaps:
            room = seg.duration
        else:
            remaining = max(total_duration, seg.end) - seg.start
            room = remaining if remaining > 0 else seg.duration

        room = max(0.0, room)
        # A tight window is not expanded; it is reported, because the honest fix is a
        # shorter translation or a slightly faster read, not a collision.
        # Tight = the room is less than the line's own duration (it must be compressed or
        # shortened), or so short that nothing sensible fits. A percentage threshold missed
        # exactly the 0.3 s segments that motivated this rule.
        seg.window_tight = room + 1e-6 < seg.duration or room < 0.4
        seg.window = round(room, 3)
        out.append(seg.window)
    return out


def map_voices(speakers: list[str], voices: list[str], fallback: str) -> dict[str, str]:
    """One voice per speaker, stable for the whole video, no voice reused until forced."""
    if not voices:
        return dict.fromkeys(speakers, fallback)
    mapping: dict[str, str] = {}
    for i, speaker in enumerate(speakers):
        mapping[speaker] = voices[i % len(voices)]
    return mapping


def stats(segments: list[Segment]) -> dict:
    """A few numbers worth printing before an expensive run."""
    by_speaker: dict[str, dict] = {}
    for seg in segments:
        entry = by_speaker.setdefault(seg.speaker, {"segments": 0, "seconds": 0.0, "chars": 0})
        entry["segments"] += 1
        entry["seconds"] = round(entry["seconds"] + seg.duration, 2)
        entry["chars"] += len(seg.speech)
    return {
        "segments": len(segments),
        "speech_seconds": round(sum(s.duration for s in segments), 2),
        "chars": sum(len(s.speech) for s in segments),
        "speakers": by_speaker,
    }


def shift(segments: list[Segment], offset: float) -> None:
    """Move every timing by `offset` — needed when ASR ran on a trimmed audio slice."""
    for seg in segments:
        seg.start = round(seg.start + offset, 3)
        seg.end = round(seg.end + offset, 3)
        for word in seg.words:
            word.start = round(word.start + offset, 3)
            word.end = round(word.end + offset, 3)
