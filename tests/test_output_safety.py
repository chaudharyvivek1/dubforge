"""Publishing an output: never leave a plausible-looking broken file, never lose a good one."""

from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, pipeline
from dubforge.config import Settings


def _settings(tmp_path, **extra) -> Settings:
    base = {
        "source_lang": "en",
        "target_lang": "hi",
        "asr": "stub",
        "translator": "stub",
        "tts": "stub",
        "separator": "none",
        "script": demo.DEMO_SCRIPT,
        "workdir": str(tmp_path / "work"),
        "cache": False,
    }
    base.update(extra)
    return Settings(**base)


@needs_ffmpeg
def test_an_existing_output_is_not_replaced_without_permission(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    out = tmp_path / "out.mp4"
    pipeline.dub(clip, _settings(tmp_path), output=out)
    before = out.read_bytes()

    with pytest.raises(pipeline.DubError, match="already exists"):
        pipeline.dub(clip, _settings(tmp_path), output=out)
    assert out.read_bytes() == before, "the refused run must not have touched the file"


@needs_ffmpeg
def test_overwriting_is_explicit(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    out = tmp_path / "out.mp4"
    pipeline.dub(clip, _settings(tmp_path), output=out)
    result = pipeline.dub(clip, _settings(tmp_path, overwrite_output=True), output=out)
    assert Path(result.output) == out


@needs_ffmpeg
def test_a_failed_mux_leaves_no_partial_file_and_keeps_the_previous_output(tmp_path, monkeypatch):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    out = tmp_path / "out.mp4"
    pipeline.dub(clip, _settings(tmp_path), output=out)
    before = out.read_bytes()

    from dubforge import ffmpeg

    def broken(*args, **kwargs):
        raise ffmpeg.FFmpegError("mux failed on purpose")

    monkeypatch.setattr(ffmpeg, "mux", broken)
    # `stage_cache=False` on purpose: with the cache on, the mux is reused and never runs, so
    # the failure path this test exists for would not be exercised at all.
    with pytest.raises((ffmpeg.FFmpegError, pipeline.DubError)):
        pipeline.dub(clip, _settings(tmp_path, overwrite_output=True, stage_cache=False), output=out)

    assert out.read_bytes() == before, "a failed regeneration must not destroy the good file"
    assert not list(tmp_path.glob("*.partial*")), "an incomplete mux must not survive"


@needs_ffmpeg
def test_a_truncated_mux_is_refused_before_publishing(tmp_path, monkeypatch):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    out = tmp_path / "out.mp4"
    from dubforge import ffmpeg

    real_mux = ffmpeg.mux

    def short_mux(source, audio, destination, bitrate=0, **kwargs):
        real_mux(source, audio, destination, **kwargs)
        Path(destination).write_bytes(b"")  # a mux that produced nothing usable

    monkeypatch.setattr(ffmpeg, "mux", short_mux)
    with pytest.raises(pipeline.DubError):
        pipeline.dub(clip, _settings(tmp_path), output=out)
    assert not out.exists(), "a file that cannot be read back must not be published"


@needs_ffmpeg
def test_the_published_file_is_readable_and_has_audio(tmp_path):
    from dubforge import ffmpeg

    clip = demo.build_clip(tmp_path / "clip.mp4")
    out = tmp_path / "out.mp4"
    result = pipeline.dub(clip, _settings(tmp_path), output=out)
    info = ffmpeg.probe(result.output)
    assert info.has_audio and info.has_video
    assert info.duration > 1.0


@needs_ffmpeg
def test_regeneration_publishes_a_new_file_and_leaves_the_old_one(tmp_path):
    from dubforge.jobs import JobStore

    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    result = pipeline.dub(clip, settings, output=tmp_path / "first.mp4")
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
        qc={"ok": result.ok, "failures": []},
    )

    before = Path(result.output).read_bytes()
    again = pipeline.regenerate(job.id, settings, edits={"0": "संपादित पंक्ति।"})
    assert Path(again.output) != Path(result.output)
    assert Path(result.output).read_bytes() == before
