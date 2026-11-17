"""Timeline rules and word alignment, as behaviour rather than prose.

`docs/quality.md` states the rules; these tests are the rules. Each case from the 0.3.0
brief gets a deterministic segment layout, so a change to `timeline.windows` that breaks one
of them fails here rather than in a rendered dub.

The bug these exist for: the old implementation floored a window at
`min(duration * max_stretch, max(duration, 0.4))`, which could *exceed* the room before the
next line — a 0.3 s segment followed 0.35 s later got a 0.4 s window and collided by 0.05 s.
"""

import pytest

from dubforge import alignment, timeline
from dubforge.media import Segment, Word


def seg(index, start, end, text="line", speaker="SPEAKER_00") -> Segment:
    return Segment(index=index, start=start, end=end, text=text, speaker=speaker)


def test_consecutive_sentences_use_the_silence_after_them():
    segments = [seg(0, 0.0, 2.0), seg(1, 2.6, 4.0)]
    windows = timeline.windows(segments, total_duration=6.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(2.48)  # 2.6 − 0.12
    assert windows[1] == pytest.approx(3.4)  # rest of the runtime
    assert not segments[0].overlaps_next


def test_a_window_never_exceeds_the_room_before_the_next_line():
    """The 0.3.0 bug: a short segment used to get a floor bigger than its room."""
    segments = [seg(0, 0.0, 0.30), seg(1, 0.35, 1.0)]
    windows = timeline.windows(segments, total_duration=1.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(0.23)  # 0.35 − 0.12, not 0.4
    assert windows[0] <= segments[1].start - segments[0].start
    assert segments[0].window_tight is True, "a squeezed line must say so"


def test_overlapping_dialogue_is_preserved_not_serialised():
    """Two people talking at once cannot be serialised without cutting one off."""
    segments = [seg(0, 0.0, 3.0, speaker="A"), seg(1, 2.0, 4.0, speaker="B")]
    windows = timeline.windows(segments, total_duration=5.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(3.0), "the overlap must not shorten the first line"
    assert segments[0].overlaps_next is True
    assert windows[1] == pytest.approx(3.0)


def test_simultaneous_speakers_are_both_kept():
    segments = [seg(0, 0.0, 2.0, speaker="A"), seg(1, 0.0, 2.0, speaker="B")]
    windows = timeline.windows(segments, total_duration=2.0, tail_gap=0.12)
    assert windows == [pytest.approx(2.0), pytest.approx(2.0)]
    assert segments[0].overlaps_next is True, "the first line overlaps the second"
    assert segments[1].overlaps_next is False, "the last line has no successor to overlap"


def test_an_asr_end_that_runs_past_the_next_start_is_flagged():
    """Bad end timestamps are the transcription's fault, not the dub's."""
    segments = [seg(0, 0.0, 2.5), seg(1, 2.2, 4.0)]
    windows = timeline.windows(segments, total_duration=4.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(2.5)  # its own duration, treated as an overlap
    assert segments[0].overlaps_next is True


def test_a_very_short_segment_gets_its_room_and_is_marked_tight():
    segments = [seg(0, 0.0, 0.18), seg(1, 0.30, 1.0)]
    windows = timeline.windows(segments, total_duration=1.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(0.18)
    assert segments[0].window_tight is True


def test_the_last_line_gets_the_remaining_runtime():
    segments = [seg(0, 0.0, 1.0), seg(1, 5.0, 6.0)]
    windows = timeline.windows(segments, total_duration=10.0, tail_gap=0.12)
    assert windows[1] == pytest.approx(5.0)  # 10 − 5, not stretched


def test_a_last_line_past_the_runtime_falls_back_to_its_own_duration():
    segments = [seg(0, 0.0, 1.0), seg(1, 9.0, 12.0)]
    windows = timeline.windows(segments, total_duration=10.0, tail_gap=0.12)
    assert windows[1] == pytest.approx(3.0)  # its own duration, because the runtime ran out


def test_a_tail_gap_is_always_respected_between_lines():
    segments = [seg(0, 0.0, 1.0), seg(1, 1.10, 2.0)]
    windows = timeline.windows(segments, total_duration=3.0, tail_gap=0.12)
    assert windows[0] == pytest.approx(0.98)
    assert windows[0] < segments[1].start - segments[0].start


def test_speaking_speed_is_a_fit_concern_not_a_window_concern():
    """The window is language-independent; only the required rate depends on the language."""
    from dubforge import fit

    segments = [seg(0, 0.0, 2.0)]
    timeline.windows(segments, total_duration=4.0)
    window = segments[0].window
    english = fit.fit("The quick brown fox jumps over the lazy dog", window, "en")
    hindi = fit.fit("तेज़ भूरी लोमड़ी आलसी कुत्ते के ऊपर कूदती है", window, "hi")
    assert english.estimate != hindi.estimate, "two languages must not share one estimate"
    assert english.window == hindi.window, "the window is a property of the source, not the target"
    assert window == segments[0].window


# --------------------------------------------------------------------- alignment


def test_measured_alignment_is_stored_and_validated():
    segment = seg(0, 0.0, 2.0, text="hello world")
    words = [Word(start=0.0, end=0.6, text="hello"), Word(start=0.7, end=1.4, text="world")]
    assert alignment.attach_measured(segment, words, "elevenlabs") is True
    assert segment.alignment == alignment.MEASURED
    assert [word.text for word in segment.target_words] == ["hello", "world"]
    assert "elevenlabs" in segment.alignment_note


def test_out_of_order_timings_are_rejected_not_smoothed():
    segment = seg(0, 0.0, 2.0, text="hello world")
    words = [Word(start=1.0, end=1.5, text="hello"), Word(start=0.2, end=0.9, text="world")]
    assert alignment.attach_measured(segment, words, "provider") is False
    assert segment.alignment == alignment.NONE
    assert segment.target_words == []
    assert "failed validation" in segment.alignment_note


def test_alignment_outside_the_segment_is_rejected():
    segment = seg(0, 5.0, 6.0, text="hello")
    words = [Word(start=0.0, end=0.5, text="hello")]
    assert alignment.attach_measured(segment, words, "provider") is False


def test_zero_duration_words_are_rejected():
    segment = seg(0, 0.0, 2.0, text="hello")
    words = [Word(start=0.5, end=0.5, text="hello")]
    assert alignment.attach_measured(segment, words, "provider") is False


def test_empty_alignment_is_a_reason_not_a_crash():
    segment = seg(0, 0.0, 2.0, text="hello")
    assert alignment.attach_measured(segment, [], "provider") is False
    assert segment.alignment == alignment.NONE


def test_no_code_path_invents_word_timings():
    """There must be no "distribute evenly" helper: an estimate dressed as a measurement."""
    import inspect

    # Docstrings may discuss interpolation (this file does, to forbid it), so scan the code
    # objects only: a helper that spreads a duration across words would live there.
    code = "\n".join(
        inspect.getsource(member)
        for member in vars(alignment).values()
        if inspect.isfunction(member)
    )
    for forbidden in ("len(words) /", "/ len(text)", "evenly", "i / n", "index / len"):
        assert forbidden not in code, f"alignment.py contains {forbidden!r} in code"


def test_coverage_reports_the_honest_ratio():
    measured = seg(0, 0.0, 1.0)
    measured.alignment = alignment.MEASURED
    plain = seg(1, 1.0, 2.0)
    summary = alignment.coverage([measured, plain])
    assert summary == {"segments": 2, "measured": 1, "ratio": 0.5, "sentence_level_only": 1}


def test_a_provider_without_timestamps_says_so():
    from dubforge.config import Settings
    from dubforge.providers import registry

    provider = registry.create("tts", "stub", Settings())
    assert hasattr(provider, "synthesize_with_timestamps"), (
        "the base class must offer the hook so a caller never has to guess"
    )
