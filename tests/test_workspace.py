"""Workspace isolation: two jobs must never write into each other's directory."""

import json
import os
import time

import pytest

from dubforge.workspace import (
    JobWorkspace,
    WorkspaceError,
    atomic_write_text,
    open_job,
    path_hash,
    slug,
)


def _touch(path, data: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------- identity


def test_same_filename_in_different_folders_gets_different_directories(tmp_path):
    a = _touch(tmp_path / "class9" / "lesson.mp4")
    b = _touch(tmp_path / "class10" / "lesson.mp4")
    job_a = open_job(tmp_path / "work", a, fingerprint="fa")
    job_b = open_job(tmp_path / "work", b, fingerprint="fb")
    try:
        assert job_a.path != job_b.path
        assert job_a.path.parent == job_b.path.parent
    finally:
        job_a.release()
        job_b.release()


def test_slug_is_safe_and_path_hash_is_stable(tmp_path):
    assert slug("../../etc/passwd") == "etc-passwd"
    assert slug("") == "job"
    assert slug("a" * 200) == "a" * 48
    a = _touch(tmp_path / "lesson.mp4")
    assert path_hash(a) == path_hash(a)
    assert path_hash(a) != path_hash(_touch(tmp_path / "other" / "lesson.mp4"))


# ------------------------------------------------------------------ concurrency


def test_a_second_live_job_gets_its_own_directory(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    first = open_job(tmp_path / "work", source, fingerprint="f1")
    second = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        assert first.path != second.path
        assert second.path.name.endswith("-2")
        # each has its own lock and manifest
        assert (first.path / ".lock").exists()
        assert (second.path / ".lock").exists()
    finally:
        first.release()
        second.release()


def test_a_dead_process_lock_is_reclaimed(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    first = open_job(tmp_path / "work", source, fingerprint="f1")
    base = first.path
    # Simulate a crash: leave the lock behind, with a pid that cannot exist.
    first.release()
    (base / ".lock").write_text(
        json.dumps({"pid": 999_999_999, "host": "gone", "started": time.time()}), encoding="utf-8"
    )
    if os.name == "nt":  # liveness is judged by age there, so age it instead
        old = time.time() - 90_000
        os.utime(base / ".lock", (old, old))
    reclaimed = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        assert reclaimed.path == base
        assert any("stale lock" in note for note in reclaimed.notes)
    finally:
        reclaimed.release()


def test_an_ancient_lock_is_reclaimed_even_without_a_pid(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1")
    base = job.path
    job.release()
    (base / ".lock").write_text("", encoding="utf-8")
    old = time.time() - 90_000
    os.utime(base / ".lock", (old, old))
    again = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        assert again.path == base
    finally:
        again.release()


# --------------------------------------------------------------------- resuming


def test_an_interrupted_job_resumes_in_place(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1")
    (job.path / "dub.wav").write_bytes(b"partial")
    job.mark("transcribe", True, "3 lines")
    job.release()

    resumed = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        assert resumed.path == job.path
        assert any("resuming" in note for note in resumed.notes)
        assert (resumed.path / "dub.wav").exists()
        assert resumed.read_manifest()["steps"]["transcribe"]["ok"] is True
    finally:
        resumed.release()


def test_changed_content_clears_the_previous_artefacts(tmp_path):
    source = _touch(tmp_path / "lesson.mp4", b"first cut")
    job = open_job(tmp_path / "work", source, fingerprint="old")
    (job.path / "dub.wav").write_bytes(b"stale")
    (job.path / "tts").mkdir()
    (job.path / "tts" / "00000.wav").write_bytes(b"stale")
    job.finished(qc_ok=True)
    job.release()

    _touch(source, b"second cut, different bytes")
    fresh = open_job(tmp_path / "work", source, fingerprint="new")
    try:
        assert fresh.path == job.path
        assert not (fresh.path / "dub.wav").exists()
        assert not (fresh.path / "tts").exists()
        assert any("content changed" in note for note in fresh.notes)
        assert fresh.read_manifest()["fingerprint"] == "new"
    finally:
        fresh.release()


def test_cache_lives_outside_the_job_directory(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1", cache_key="cachekey")
    try:
        assert job.cache_dir.parent.name == "cache"
        assert not job.contains(job.cache_dir)
        assert job.cache_dir.name == "cachekey"
    finally:
        job.release()


# ---------------------------------------------------------------------- safety


def test_file_refuses_to_escape_the_job_directory(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        with pytest.raises(WorkspaceError):
            job.file("../escape.wav")
        with pytest.raises(WorkspaceError):
            job.file("/etc/passwd")
        assert job.file("tts/00001.wav").parent.name == "tts"
    finally:
        job.release()


def test_cleanup_stays_inside_the_job_and_keeps_what_matters(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1")
    try:
        (job.path / "tts").mkdir()
        (job.path / "tts" / "0.wav").write_bytes(b"a")
        (job.path / "stems").mkdir()
        (job.path / "stems" / "bed.wav").write_bytes(b"a")
        (job.path / "dub.part0.wav").write_bytes(b"a")
        (job.path / "qc.json").write_text("{}", encoding="utf-8")
        outside = tmp_path / "precious.txt"
        outside.write_text("do not delete", encoding="utf-8")

        removed = job.cleanup()
        assert "tts" in removed and "stems" in removed
        assert not (job.path / "tts").exists()
        assert (job.path / "qc.json").exists()
        assert outside.read_text(encoding="utf-8") == "do not delete"
    finally:
        job.release()


def test_cleanup_skips_a_symlink_that_points_outside(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = open_job(tmp_path / "work", source, fingerprint="f1")
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    try:
        try:
            (job.path / "tts").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted here")
        job.cleanup()
        assert (outside / "keep.txt").exists(), "cleanup followed a symlink out of the job"
    finally:
        job.release()


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "manifest.json"
    atomic_write_text(target, '{"a": 1}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]


def test_manifest_survives_a_corrupt_file(tmp_path):
    source = _touch(tmp_path / "lesson.mp4")
    job = JobWorkspace(path=tmp_path / "job", cache_dir=tmp_path / "cache", source=str(source))
    job.path.mkdir(parents=True)
    (job.path / "manifest.json").write_text("}{ broken", encoding="utf-8")
    assert job.read_manifest() == {}
    job.write_manifest(fingerprint="f")
    assert job.read_manifest()["fingerprint"] == "f"
