"""Stage-level reuse: the mix and the mux are cached against their real inputs, or not at all."""

import json
from pathlib import Path

from conftest import needs_ffmpeg

from dubforge import demo, pipeline
from dubforge.config import Settings
from dubforge.mix import StageCache, mix_key, mux_key, summarise_timings


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


def _wav(tmp_path, name="a.wav", data=b"RIFF" + b"x" * 4096) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------------ keys


def test_the_mix_key_covers_every_input_that_changes_the_mix(tmp_path):
    dialogue, bed = _wav(tmp_path, "d.wav"), _wav(tmp_path, "b.wav")
    base = {
        "voice_gain_db": 0.0,
        "bed_gain_db": -6.0,
        "duck": True,
        "loudness_lufs": -16.0,
        "true_peak_db": -1.5,
        "total_duration": 9.2,
    }
    reference = mix_key(dialogue, bed, **base)

    assert mix_key(dialogue, bed, **{**base, "voice_gain_db": 3.0}) != reference
    assert mix_key(dialogue, bed, **{**base, "bed_gain_db": -9.0}) != reference
    assert mix_key(dialogue, bed, **{**base, "duck": False}) != reference
    assert mix_key(dialogue, bed, **{**base, "loudness_lufs": -14.0}) != reference
    assert mix_key(dialogue, bed, **{**base, "true_peak_db": -2.0}) != reference
    assert mix_key(dialogue, bed, **{**base, "total_duration": 10.0}) != reference
    # content, not name: a differently-named file with the same bytes is the same bed
    assert mix_key(dialogue, _wav(tmp_path, "same-bytes.wav"), **base) == reference
    assert (
        mix_key(dialogue, _wav(tmp_path, "other.wav", b"RIFF" + b"y" * 4096), **base) != reference
    )
    assert mix_key(_wav(tmp_path, "newd.wav", b"RIFF" + b"z" * 4096), bed, **base) != reference


def test_a_changed_dialogue_invalidates_the_mix(tmp_path):
    """This is the honest limit: a spliced dialogue moves the loudness gain, so it misses."""
    dialogue = _wav(tmp_path, "d.wav")
    arguments = {
        "voice_gain_db": 0.0,
        "bed_gain_db": -6.0,
        "duck": True,
        "loudness_lufs": -16.0,
        "true_peak_db": -1.5,
        "total_duration": 9.2,
    }
    before = mix_key(dialogue, None, **arguments)
    dialogue.write_bytes(b"RIFF" + b"different bytes")
    assert mix_key(dialogue, None, **arguments) != before


def test_the_mux_key_covers_the_container_inputs(tmp_path):
    mixed, source = _wav(tmp_path, "m.wav"), _wav(tmp_path, "s.mp4")
    reference = mux_key(mixed, source, audio_bitrate="192k", has_video=True)
    assert mux_key(mixed, source, audio_bitrate="128k", has_video=True) != reference
    assert mux_key(mixed, source, audio_bitrate="192k", has_video=False) != reference
    assert (
        mux_key(
            mixed,
            _wav(tmp_path, "s2.mp4", b"RIFF" + b"v" * 4096),
            audio_bitrate="192k",
            has_video=True,
        )
        != reference
    )


# ----------------------------------------------------------------------- store


def test_a_stage_artifact_round_trips_and_is_checksummed(tmp_path):
    cache = StageCache(tmp_path / "mix", ".wav")
    source = _wav(tmp_path)
    record = cache.save("abc123", source, {"note": "x"})
    found = cache.find("abc123")
    assert found is not None
    assert Path(found.artifact).read_bytes() == source.read_bytes()
    assert found.meta["note"] == "x"
    assert record.sha256 == found.sha256


def test_a_truncated_stage_artifact_is_a_miss(tmp_path):
    cache = StageCache(tmp_path / "mix", ".wav")
    record = cache.save("abc123", _wav(tmp_path))
    Path(record.artifact).write_bytes(b"")
    assert cache.find("abc123") is None


def test_a_stage_without_metadata_is_a_miss(tmp_path):
    cache = StageCache(tmp_path / "mix", ".wav")
    cache.root.mkdir(parents=True, exist_ok=True)
    (cache.root / "orphan.wav").write_bytes(b"RIFF" + b"y" * 2048)
    assert cache.find("orphan") is None


def test_cleanup_drops_the_largest_first(tmp_path):
    cache = StageCache(tmp_path / "mix", ".wav")
    cache.save("small", _wav(tmp_path, "small.wav", b"RIFF" + b"s" * 2048))
    cache.save("big", _wav(tmp_path, "big.wav", b"RIFF" + b"b" * 8192))
    dry = cache.cleanup(max_bytes=4096, dry_run=True)
    assert dry["removed"] == ["big.wav"]
    assert cache.find("big") is not None, "a dry run must not delete anything"
    real = cache.cleanup(max_bytes=4096, dry_run=False)
    assert real["removed"] == ["big.wav"]
    assert cache.find("big") is None
    assert cache.find("small") is not None


# -------------------------------------------------------------------- timings


def test_the_timing_summary_names_the_slowest_stage():
    class Step:
        def __init__(self, name, seconds):
            self.name, self.seconds = name, seconds

    summary = summarise_timings([Step("synthesise", 2.0), Step("mix", 0.5), Step("reuse", 0.0)])
    assert summary["slowest_stage"] == "synthesise"
    assert summary["slowest_seconds"] == 2.0
    assert summary["total_stage_seconds"] == 2.5
    assert "reuse" not in summary["stage_seconds"], "a zero-second step is not a measurement"


# ------------------------------------------------------------------- pipeline


@needs_ffmpeg
def test_an_unchanged_run_reuses_every_stage(tmp_path, monkeypatch):
    """Nothing changed, so nothing is recomputed — and the output is still byte-identical."""
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")

    calls = []
    from dubforge import ffmpeg

    for name in ("loudnorm", "duck_and_mix", "mux", "assemble_dub"):
        original = getattr(ffmpeg, name)
        monkeypatch.setattr(
            ffmpeg, name, lambda *a, _n=name, _f=original, **k: (calls.append(_n), _f(*a, **k))[1]
        )

    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.dialogue_mode == "reused"
    assert second.mix_mode == "reused"
    assert second.mux_mode == "reused"
    assert calls == [], f"a no-op regeneration ran {calls}"
    assert Path(second.output).read_bytes() == Path(first.output).read_bytes()


@needs_ffmpeg
def test_a_changed_loudness_target_rebuilds_the_mix_only(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    pipeline.dub(clip, _settings(tmp_path), output=tmp_path / "a.mp4")
    other = pipeline.dub(clip, _settings(tmp_path, loudness_lufs=-20.0), output=tmp_path / "b.mp4")
    assert other.dialogue_mode == "reused", "the dialogue did not change"
    assert other.mix_mode == "rebuilt", "the loudness target did"
    assert other.mux_mode == "rebuilt"


@needs_ffmpeg
def test_a_spliced_dialogue_always_rebuilds_the_mix(tmp_path):
    """Documented limit: a changed line moves the global gain, so the mix is not spliced."""
    from dubforge.jobs import JobStore

    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    result = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
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

    again = pipeline.regenerate(job.id, settings, edits={"1": "यह बदली हुई पंक्ति है।"})
    assert again.dialogue_mode == "spliced"
    assert again.mix_mode == "rebuilt"
    assert again.mux_mode == "rebuilt"


@needs_ffmpeg
def test_the_review_reports_the_modes_and_the_timings(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    work = json.loads(Path(second.review).read_text(encoding="utf-8"))["work"]
    assert work["mix_mode"] == "reused" and work["mux_mode"] == "reused"
    assert work["rebuilt_stages"] == [], "nothing was rebuilt, and the report must say so"
    assert "slowest_stage" in work and "total_stage_seconds" in work


@needs_ffmpeg
def test_the_stage_cache_can_be_switched_off(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    pipeline.dub(clip, _settings(tmp_path), output=tmp_path / "a.mp4")
    again = pipeline.dub(clip, _settings(tmp_path, stage_cache=False), output=tmp_path / "b.mp4")
    assert again.mix_mode == "rebuilt"
    assert again.mux_mode == "rebuilt"
