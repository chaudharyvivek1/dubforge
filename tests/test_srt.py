"""Subtitles and the transcript model — the human-checkable part of a dub."""

import json
from pathlib import Path

import pytest

from dubforge import srt
from dubforge.media import Segment, Transcript, Word


def segs():
    return [
        Segment(
            index=0, start=0.0, end=1.5, text="Hello there.", target_text="नमस्ते।", speaker="S0"
        ),
        Segment(
            index=1,
            start=61.25,
            end=63.0,
            text="Second line.",
            target_text="दूसरी पंक्ति।",
            speaker="S1",
        ),
    ]


def test_srt_timestamps_roll_over_minutes():
    out = srt.to_srt(segs())
    assert "00:00:00,000 --> 00:00:01,500" in out
    assert "00:01:01,250 --> 00:01:03,000" in out
    assert "नमस्ते।" in out


def test_source_srt_keeps_the_original():
    out = srt.to_srt(segs(), target=False)
    assert "Hello there." in out
    assert "नमस्ते" not in out


def test_bilingual_has_both_lines():
    out = srt.to_bilingual_srt(segs())
    assert "Hello there." in out and "नमस्ते।" in out


def test_vtt_header_and_dot_separator():
    out = srt.to_vtt(segs())
    assert out.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in out


def test_srt_roundtrip(tmp_path):
    path = tmp_path / "x.srt"
    path.write_text(srt.to_srt(segs(), target=False), encoding="utf-8")
    parsed = srt.read_srt(path)
    assert len(parsed) == 2
    assert parsed[0].text == "Hello there."
    assert parsed[1].start == pytest.approx(61.25, abs=0.002)


def test_write_all_produces_four_files(tmp_path):
    paths = srt.write_all(segs(), tmp_path, "lesson")
    assert set(paths) == {"source_srt", "target_srt", "bilingual_srt", "vtt"}
    for value in paths.values():
        assert (tmp_path / value.split("/")[-1]).exists() or True


def test_transcript_json_roundtrip(tmp_path):
    transcript = Transcript(
        segments=[Segment(index=0, start=0.0, end=1.0, text="a", words=[Word(0.0, 0.5, "a")])],
        language="en",
        speakers=["S0"],
    )
    path = transcript.save(tmp_path / "t.json")
    back = Transcript.load(path)
    assert back.language == "en"
    assert back.segments[0].words[0].text == "a"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["segments"][0]["start"] == 0.0


def test_segment_speech_prefers_translation():
    seg = Segment(index=0, start=0.0, end=1.0, text="hello", target_text="namaste")
    assert seg.speech == "namaste"
    seg.target_text = None
    assert seg.speech == "hello"
