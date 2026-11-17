"""The review artifact and the three new per-line QC checks."""

import json
from pathlib import Path

from conftest import needs_ffmpeg

from dubforge import demo, pipeline, qc
from dubforge.config import Settings
from dubforge.media import Segment


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
        **extra,
    )


@needs_ffmpeg
def test_review_json_carries_every_field_a_reviewer_needs(demo_clip, tmp_path):
    result = pipeline.dub(demo_clip, _settings(tmp_path), output=tmp_path / "out.mp4")
    payload = json.loads(Path(result.review).read_text(encoding="utf-8"))

    for field in ("generated_at", "target_lang", "providers_used", "alignment", "qc", "segments"):
        assert field in payload
    row = payload["segments"][0]
    for field in (
        "index",
        "seg_id",
        "speaker",
        "source_text",
        "target_text",
        "voice",
        "source",
        "target",
        "fit",
        "audio",
        "alignment",
        "regenerated",
        "warnings",
    ):
        assert field in row, f"{field} missing from the review row"
    assert row["source"]["start"] <= row["source"]["end"]
    assert row["fit"]["verdict"] in ("ok", "stretch", "rewrite")
    assert row["alignment"]["sentence_level_only"] is True
    assert payload["note"].startswith("One row per line")


@needs_ffmpeg
def test_review_json_records_reuse_and_regeneration(demo_clip, tmp_path):
    settings = _settings(tmp_path)
    pipeline.dub(demo_clip, settings, output=tmp_path / "a.mp4")
    second = pipeline.dub(demo_clip, settings, output=tmp_path / "b.mp4")
    payload = json.loads(Path(second.review).read_text(encoding="utf-8"))
    assert payload["reused_segments"] == 3
    assert payload["rendered_segments"] == 0
    assert all(row["regenerated"] is False for row in payload["segments"])


# --------------------------------------------------------------------- the checks


def _segment(index=0, speech="hello world", rendered=2.0, speed=1.0) -> Segment:
    segment = Segment(index=index, start=0.0, end=2.0, text=speech)
    segment.target_text = speech
    segment.rendered_duration = rendered
    segment.speed = speed
    return segment


def _checks(segments, **kwargs) -> dict:
    report = qc.report(segments=segments, target_lang="en", **kwargs)
    return {check["check"]: check for check in report["checks"]}


def test_a_truncated_render_is_flagged():
    checks = _checks([_segment(speech="a sentence of quite reasonable length", rendered=0.3)])
    assert checks["short_tts_output"]["ok"] is False
    assert checks["short_tts_output"]["declared"] == "warn"
    assert "shorter than their text" in checks["short_tts_output"]["detail"]


def test_a_proportionate_render_passes():
    checks = _checks([_segment(speech="hello world", rendered=1.0)])
    assert checks["short_tts_output"]["ok"] is True


def test_audible_compression_is_flagged_but_only_above_the_threshold():
    assert _checks([_segment(speed=1.05)])["excessive_compression"]["ok"] is True
    loud = _checks([_segment(speed=1.2)])["excessive_compression"]
    assert loud["ok"] is False
    assert "above 1.1x" in loud["detail"] and "[0]" in loud["detail"]


def test_the_threshold_is_configurable():
    checks = _checks([_segment(speed=1.05)], max_audible_speed=1.02)
    assert checks["excessive_compression"]["ok"] is False


@needs_ffmpeg
def test_a_line_that_is_mostly_silence_is_flagged(tmp_path):
    from dubforge import ffmpeg

    silent = str(tmp_path / "silent.wav")
    ffmpeg.run(
        ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2", "-c:a", "pcm_s16le", silent]
    )
    segment = _segment(rendered=2.0)
    segment.audio_path = silent
    checks = _checks([segment])
    assert checks["silence_inside_line"]["ok"] is False
    assert "mostly silent" in checks["silence_inside_line"]["detail"]


@needs_ffmpeg
def test_a_real_render_is_not_flagged_as_silent(demo_clip, tmp_path):
    result = pipeline.dub(demo_clip, _settings(tmp_path), output=tmp_path / "out.mp4")
    checks = {check["check"]: check for check in result.qc["checks"]}
    assert checks["silence_inside_line"]["ok"] is True
    assert result.qc["metrics"]["lines_silence_measured"] >= 1


def test_silence_measurement_is_bounded():
    """Measuring silence on a thousand lines must not cost a thousand decodes."""
    segments = [_segment(index=index) for index in range(50)]
    for segment in segments:
        segment.audio_path = "/nonexistent.wav"
    checks = _checks(segments, silence_sample=5)
    assert checks["silence_inside_line"]["ok"] is True


def test_the_new_checks_do_not_duplicate_existing_ones():
    names = [
        check["check"] for check in qc.report(segments=[_segment()], target_lang="en")["checks"]
    ]
    assert len(names) == len(set(names)), "a check name appears twice"
    assert {"speech_rate", "excessive_compression"} <= set(names)
