"""Segmentation, speaker assignment, windows and voice mapping."""

from dubforge import timeline
from dubforge.media import Segment, Word


def words(*specs):
    return [Word(start=s, end=e, text=t) for s, e, t in specs]


def test_merge_words_breaks_on_silence():
    ws = words((0.0, 0.4, "Hello"), (0.4, 0.9, "there."), (2.0, 2.4, "Second"), (2.4, 2.9, "line."))
    segments = timeline.merge_words(ws, gap=0.7)
    assert len(segments) == 2
    assert segments[0].text == "Hello there."
    assert segments[0].start == 0.0
    assert segments[1].start == 2.0


def test_merge_words_respects_max_duration():
    ws = [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=f"w{i}") for i in range(40)]
    segments = timeline.merge_words(ws, gap=1.0, max_duration=4.0)
    assert all(s.duration <= 4.5 for s in segments)
    assert sum(len(s.words) for s in segments) == 40


def test_merge_words_breaks_when_speaker_changes():
    ws = [
        Word(start=0.0, end=0.4, text="mine", speaker="SPEAKER_00"),
        Word(start=0.4, end=0.9, text="yours", speaker="SPEAKER_01"),
    ]
    segments = timeline.merge_words(ws, gap=5.0)
    assert len(segments) == 2
    assert [s.speaker for s in segments] == ["SPEAKER_00", "SPEAKER_01"]


def test_assign_speakers_picks_largest_overlap():
    segments = [
        Segment(index=0, start=0.0, end=2.0, text="a"),
        Segment(index=1, start=2.0, end=4.0, text="b"),
    ]
    turns = [(0.0, 1.2, "SPEAKER_01"), (1.2, 2.5, "SPEAKER_00"), (2.5, 4.0, "SPEAKER_01")]
    timeline.assign_speakers(segments, turns)
    assert segments[0].speaker == "SPEAKER_01"  # 1.2s of overlap beats 0.8s
    assert segments[1].speaker == "SPEAKER_01"


def test_windows_never_reach_into_the_next_line():
    segments = [
        Segment(index=0, start=0.0, end=1.0, text="a"),
        Segment(index=1, start=2.0, end=3.0, text="b"),
    ]
    timeline.windows(segments, total_duration=10.0, tail_gap=0.1)
    assert segments[0].window <= 2.0 - 0.1 + 1e-6
    assert segments[1].window > 0


def test_last_line_gets_room_to_breathe():
    segments = [Segment(index=0, start=8.0, end=9.0, text="a")]
    timeline.windows(segments, total_duration=12.0, tail_gap=0.1)
    assert segments[0].window >= 1.0


def test_map_voices_is_stable_and_reuses_only_when_forced():
    mapping = timeline.map_voices(
        ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], ["v1", "v2"], "fallback"
    )
    assert mapping["SPEAKER_00"] == "v1"
    assert mapping["SPEAKER_01"] == "v2"
    assert mapping["SPEAKER_02"] == "v1"
    assert timeline.map_voices(["A"], [], "fb") == {"A": "fb"}


def test_shift_moves_words_too():
    seg = Segment(index=0, start=1.0, end=2.0, text="a", words=[Word(start=1.0, end=1.5, text="a")])
    timeline.shift([seg], 10.0)
    assert seg.start == 11.0
    assert seg.words[0].start == 11.0


def test_stats_counts_speakers():
    segments = [
        Segment(index=0, start=0.0, end=1.0, text="hello", speaker="S0"),
        Segment(index=1, start=1.0, end=3.0, text="hi there", speaker="S1"),
    ]
    stats = timeline.stats(segments)
    assert stats["segments"] == 2
    assert stats["speakers"]["S0"]["segments"] == 1
    assert stats["chars"] == len("hello") + len("hi there")
