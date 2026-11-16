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
    max_stretch: float = 2.5,
) -> list[float]:
    """Seconds each dub line may occupy, then written onto `segment.window`.

    A line may run into the silence that follows it, but never into the start of the
    next line (minus `tail_gap` so two voices do not collide). The last line gets its
    own duration or the remaining runtime, whichever is smaller, times `max_stretch`
    so a final line is not forced into a 0.4s window.
    """
    ordered = sorted(segments, key=lambda s: s.start)
    out: list[float] = []
    for i, seg in enumerate(ordered):
        if i + 1 < len(ordered):
            room = ordered[i + 1].start - tail_gap - seg.start
        else:
            remaining = max(total_duration, seg.end) - seg.start
            room = remaining if remaining > 0 else seg.duration
        room = max(room, min(seg.duration * max_stretch, max(seg.duration, 0.4)))
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
