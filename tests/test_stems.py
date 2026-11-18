"""Source-stem cache: reuse the bed, never reuse the mix, and invalidate honestly."""

import json
import time
from pathlib import Path

from conftest import needs_ffmpeg

from dubforge import demo, pipeline
from dubforge.config import Settings
from dubforge.stems import StemCache, StemRecord, cache_key


def bed(tmp_path, name="bed.wav", size=4096) -> Path:
    path = tmp_path / name
    path.write_bytes(b"RIFF" + b"b" * size)
    return path


# ------------------------------------------------------------------ the key


def test_the_key_covers_the_source_and_the_separator():
    base = cache_key("hash:1", "demucs")
    assert cache_key("hash:2", "demucs") != base
    assert cache_key("hash:1", "ffmpeg_center") != base
    assert cache_key("hash:1", "demucs", model="v4") != base
    assert cache_key("hash:1", "demucs", sample_rate=44100) != base
    assert cache_key("hash:1", "demucs") == base


def test_the_key_ignores_what_does_not_change_the_bed():
    assert cache_key("hash:1", "demucs", model="v4") == cache_key(
        "hash:1", "demucs", model="v4", keep_background=True
    )


# --------------------------------------------------------------- the store


def test_a_saved_bed_is_found_and_checksummed(tmp_path):
    cache = StemCache(tmp_path / "stems")
    key = cache_key("hash:1", "demucs")
    record = cache.save(StemRecord(key=key, bed="", bed_sha256="", duration_s=10.0), bed(tmp_path))
    found = cache.find(key)
    assert found is not None
    assert Path(found.bed).exists()
    assert found.bed_sha256 == record.bed_sha256


def test_a_corrupted_bed_is_a_miss(tmp_path):
    cache = StemCache(tmp_path / "stems")
    key = cache_key("hash:1", "demucs")
    record = cache.save(StemRecord(key=key, bed="", bed_sha256=""), bed(tmp_path))
    Path(record.bed).write_bytes(b"RIFF" + b"z" * 4096)  # same size, different bytes
    assert cache.find(key) is None


def test_an_empty_bed_is_a_miss(tmp_path):
    cache = StemCache(tmp_path / "stems")
    key = cache_key("hash:1", "demucs")
    record = cache.save(StemRecord(key=key, bed="", bed_sha256=""), bed(tmp_path))
    Path(record.bed).write_bytes(b"")
    assert cache.find(key) is None


def test_an_interrupted_write_leaves_no_record(tmp_path):
    cache = StemCache(tmp_path / "stems")
    key = cache_key("hash:1", "demucs")
    directory = cache.dir(key)
    directory.mkdir(parents=True)
    (directory / "bed.wav.tmp").write_bytes(b"half")  # killed mid-copy
    assert cache.find(key) is None


def test_one_worker_separates_at_a_time(tmp_path):
    cache = StemCache(tmp_path / "stems")
    key = cache_key("hash:1", "demucs")
    with cache.claim(key) as first:
        assert first is True
        with cache.claim(key, wait=0.0) as second:
            assert second is False
    with cache.claim(key, wait=0.0) as third:
        assert third is True


def test_stats_and_cleanup_bound_the_cache(tmp_path):
    cache = StemCache(tmp_path / "stems")
    for index in range(3):
        key = cache_key(f"hash:{index}", "demucs")
        cache.save(StemRecord(key=key, bed="", bed_sha256=""), bed(tmp_path, f"b{index}.wav"))
    info = cache.stats()
    assert info["entries"] == 3 and info["bytes"] > 0

    dry = cache.cleanup(max_bytes=1, dry_run=True)
    assert len(dry["removed"]) == 3
    assert cache.stats()["entries"] == 3, "a dry run must not delete anything"

    real = cache.cleanup(max_bytes=1, dry_run=False)
    assert len(real["removed"]) == 3
    assert cache.stats()["entries"] == 0


def test_cleanup_by_age_keeps_fresh_entries(tmp_path):
    cache = StemCache(tmp_path / "stems")
    old_key = cache_key("old", "demucs")
    new_key = cache_key("new", "demucs")
    cache.save(
        StemRecord(key=old_key, bed="", bed_sha256="", created_at=time.time() - 10 * 86400),
        bed(tmp_path, "old.wav"),
    )
    cache.save(StemRecord(key=new_key, bed="", bed_sha256=""), bed(tmp_path, "new.wav"))
    outcome = cache.cleanup(older_than_days=7, dry_run=False)
    assert outcome["removed"] == [old_key]
    assert cache.find(new_key) is not None


# ------------------------------------------------------------- in the pipeline


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


@needs_ffmpeg
def test_a_second_run_reuses_the_bed(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    assert first.stems_rendered == 1 and first.stems_reused == 0

    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.stems_reused == 1
    assert second.stems_rendered == 0
    assert second.reused_segments == 3


@needs_ffmpeg
def test_a_different_separator_does_not_reuse_the_bed(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    pipeline.dub(clip, _settings(tmp_path), output=tmp_path / "a.mp4")
    # Turning the bed off entirely is a different separation configuration, so it cannot
    # reuse the cached bed: the key must cover `keep_background`.
    other = pipeline.dub(
        clip, _settings(tmp_path, keep_background=False), output=tmp_path / "b.mp4"
    )
    assert other.stems_reused == 0, "a different separation configuration is a different bed"


@needs_ffmpeg
def test_a_different_source_does_not_reuse_the_bed(tmp_path):
    one = Path(demo.build_clip(tmp_path / "one.mp4"))
    two = tmp_path / "two.mp4"
    two.write_bytes(one.read_bytes() + b"different")
    settings = _settings(tmp_path)
    pipeline.dub(str(one), settings, output=tmp_path / "a.mp4")
    other = pipeline.dub(str(two), settings, output=tmp_path / "b.mp4")
    assert other.stems_reused == 0


@needs_ffmpeg
def test_the_stem_cache_can_be_switched_off(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    pipeline.dub(clip, _settings(tmp_path), output=tmp_path / "a.mp4")
    again = pipeline.dub(clip, _settings(tmp_path, stem_cache=False), output=tmp_path / "b.mp4")
    assert again.stems_reused == 0 and again.stems_rendered == 1


@needs_ffmpeg
def test_the_review_records_what_was_actually_rebuilt(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    payload = json.loads(Path(second.review).read_text(encoding="utf-8"))
    work = payload["work"]
    assert work["reused_renders"] == 3 and work["rendered_segments"] == 0
    assert work["stems_reused"] == 1
    assert work["cache_hit_ratio"] == 1.0
    # 0.6.0 contract: the mix is reused when the dialogue's bytes are unchanged, so ducking
    # does not run — and the report must not claim it did.
    assert work["mix_mode"] == "reused"
    assert work["rebuilt_stages"] == [], "nothing was rebuilt, so nothing may be listed"
    fresh = pipeline.dub(
        demo.build_clip(tmp_path / "fresh.mp4"),
        _settings(tmp_path, stage_cache=False),
        output=tmp_path / "c.mp4",
    )
    fresh_work = json.loads(Path(fresh.review).read_text(encoding="utf-8"))["work"]
    assert "ducking" in fresh_work["rebuilt_stages"], "a run that rebuilt the mix must say so"
