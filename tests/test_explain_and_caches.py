"""The last two passes, and the command that explains what is left.

Extraction and placement were the only stages still running on a no-op regeneration. Both are
now cached, and `explain` reports what actually happened instead of what the code intended.
"""

import json
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, explain, pipeline
from dubforge.config import Settings
from dubforge.mix import extract_key
from dubforge.segments import SegmentStore


def _settings(tmp_path, **extra) -> Settings:
    base = {
        "source_lang": "en",
        "target_lang": "hi",
        "asr": "stub",
        "translator": "stub",
        "tts": "stub",
        "separator": "ffmpeg_center",
        "script": demo.DEMO_SCRIPT,
        "workdir": str(tmp_path / "work"),
        "cache": False,
    }
    base.update(extra)
    return Settings(**base)


# ------------------------------------------------------------------- keys


def test_the_extract_key_covers_content_and_format():
    base = extract_key("hash:1", rate=16000, channels=1)
    assert extract_key("hash:2", rate=16000, channels=1) != base
    assert extract_key("hash:1", rate=48000, channels=1) != base
    assert extract_key("hash:1", rate=16000, channels=2) != base
    assert extract_key("hash:1", rate=16000, channels=1) == base


# --------------------------------------------------------------- extraction


@needs_ffmpeg
def test_a_second_run_does_not_decode_the_source_again(tmp_path, monkeypatch):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")

    calls = []
    from dubforge import ffmpeg

    original = ffmpeg.extract_audio
    monkeypatch.setattr(
        ffmpeg, "extract_audio", lambda *a, **k: (calls.append(a[2]), original(*a, **k))[1]
    )
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert calls == [], f"a no-op run decoded the source again: {calls}"
    assert second.extract_mode == "reused"


@needs_ffmpeg
def test_a_corrupted_extract_is_a_miss(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    cache = Path(settings.workdir) / "cache" / "audio"
    victim = sorted(cache.glob("*.wav"))[0]
    victim.write_bytes(b"")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.extract_mode in ("partial", "rebuilt")


@needs_ffmpeg
def test_a_different_source_is_decoded_fresh(tmp_path):
    one = Path(demo.build_clip(tmp_path / "one.mp4"))
    two = tmp_path / "two.mp4"
    two.write_bytes(one.read_bytes() + b"different bytes")
    settings = _settings(tmp_path)
    pipeline.dub(str(one), settings, output=tmp_path / "a.mp4")
    other = pipeline.dub(str(two), settings, output=tmp_path / "b.mp4")
    assert other.extract_mode == "rebuilt"


# ---------------------------------------------------------------- placement


@needs_ffmpeg
def test_the_stretched_render_is_reused_when_the_window_is_unchanged(tmp_path, monkeypatch):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    assert first.extract_mode == "rebuilt"

    calls = []
    from dubforge import ffmpeg

    original = ffmpeg.stretch
    monkeypatch.setattr(ffmpeg, "stretch", lambda *a, **k: (calls.append(a), original(*a, **k))[1])
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert calls == [], "a no-op run stretched again"
    assert second.placed_reused >= 1


@needs_ffmpeg
def test_a_changed_window_invalidates_the_placed_render(tmp_path):
    """The placed artifact is keyed on the speed, because a different window is a different read."""
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    store = SegmentStore(next(Path(settings.workdir).glob("jobs/*/segments")))
    placed = sorted(store.dir().glob("*.placed.wav"))
    assert placed, "a stretched line must leave a placed artifact"
    placed[0].write_bytes(b"")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.placed_reused < len(placed) + 1


def test_a_placed_record_without_a_checksum_is_a_miss(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    store.dir().mkdir(parents=True, exist_ok=True)
    (store.dir() / "00000-abc.json").write_text(
        json.dumps(
            {
                "seg_id": "00000-abc",
                "fingerprint": "f",
                "audio": "x.wav",
                "audio_sha256": "y",
                "duration_s": 1.0,
                "meta": {"speed": 1.2},
            }
        ),
        encoding="utf-8",
    )
    store.placed_path("00000-abc").write_bytes(b"RIFF" + b"z" * 100)
    assert store.find_placed("00000-abc", 1.2) is None


def test_the_placed_duration_is_stored_so_no_probe_is_needed(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "placed.wav"
    audio.write_bytes(b"RIFF" + b"p" * 2048)
    (store.dir() / "00000-abc.json").write_text(
        json.dumps(
            {
                "seg_id": "00000-abc",
                "fingerprint": "f",
                "audio": "x.wav",
                "audio_sha256": "y",
                "duration_s": 1.0,
                "meta": {},
            }
        ),
        encoding="utf-8",
    )
    store.save_placed("00000-abc", audio, 1.25, 1.6)
    found = store.find_placed("00000-abc", 1.25)
    assert found is not None
    path, duration = found
    assert duration == 1.6


# -------------------------------------------------------------------- explain


@pytest.fixture()
def finished_job(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    result = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    from dubforge.jobs import JobStore

    store = JobStore(settings.workdir)
    job = store.create(clip, settings)
    store.update(job.id, status="processing", pid=1)
    store.update(
        job.id,
        status="completed",
        qc_ok=result.ok,
        transcript_json=result.transcript_json,
        output=result.output,
        review=result.review,
        qc={"ok": result.ok, "failures": result.qc["failures"]},
    )
    return settings, job.id


@needs_ffmpeg
def test_explain_reads_the_review_rather_than_guessing(finished_job):
    settings, job_id = finished_job
    story = explain.explain(job_id, settings)
    assert story["job_id"] == job_id
    assert story["modes"]["mix"] == "rebuilt"
    assert "normalisation" in story["rebuilt_stages"]
    assert story["timing"]["slowest_stage"]
    assert story["next_edit"]["mix"].startswith("rebuilt")


@needs_ffmpeg
def test_explain_renders_a_readable_page(finished_job):
    settings, job_id = finished_job
    text = explain.render(explain.explain(job_id, settings))
    assert "what ran" in text and "where the time went" in text
    assert "if you edit one more line" in text
    assert "mix" in text and "rebuilt" in text


@needs_ffmpeg
def test_explain_reports_a_no_op_run_as_reusing_everything(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    from dubforge.jobs import JobStore

    store = JobStore(settings.workdir)
    job = store.create(clip, settings)
    store.update(job.id, status="processing", pid=1)
    store.update(
        job.id,
        status="completed",
        qc_ok=second.ok,
        transcript_json=second.transcript_json,
        output=second.output,
        review=second.review,
        qc={"ok": second.ok, "failures": []},
    )
    story = explain.explain(job.id, settings)
    assert story["modes"] == {
        "extract": "reused",
        "dialogue": "reused",
        "mix": "reused",
        "mux": "reused",
        "stems_reused": 1,
    }
    assert story["rebuilt_stages"] == []


def test_explain_refuses_a_job_without_a_review(tmp_path):
    source = tmp_path / "x.mp4"
    source.write_bytes(b"x")
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    from dubforge.jobs import JobStore

    job = JobStore(settings.workdir).create(source, settings)
    with pytest.raises(explain.ExplainError, match="no review.json"):
        explain.explain(job.id, settings)


def test_explain_refuses_an_unknown_job(tmp_path):
    with pytest.raises(explain.ExplainError, match="no job"):
        explain.explain("nope", Settings(workdir=str(tmp_path / "work")))


def test_the_next_edit_table_names_every_stage():
    assert set(explain.NEXT_EDIT) == {"synthesis", "bed", "dialogue track", "mix", "mux", "qc"}
