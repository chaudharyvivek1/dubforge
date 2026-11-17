"""Job identity: content, not metadata; every input that changes the result."""

import os
from pathlib import Path

import pytest

from dubforge.config import Settings
from dubforge.jobs import (
    JobError,
    JobStore,
    file_content_hash,
    request_fingerprint,
    source_fingerprint,
)


def media(tmp_path, name="clip.mp4", data=b"AAAA", mtime=1000) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


# ------------------------------------------------------------- source content


def test_different_content_with_the_same_size_and_mtime_is_a_different_source(tmp_path):
    """The 0.3.0 bug: `size:mtime` made two different videos one job."""
    a = media(tmp_path, "a.mp4", b"AAAA", mtime=1000)
    b = media(tmp_path, "b.mp4", b"BBBB", mtime=1000)
    assert source_fingerprint(a) != source_fingerprint(b)


def test_the_fingerprint_is_a_hash_plus_the_size(tmp_path):
    path = media(tmp_path)
    value = source_fingerprint(path)
    digest, size = value.split(":")
    assert len(digest) == 64 and int(size) == 4


def test_identical_content_under_another_name_is_the_same_source(tmp_path):
    """Documented policy: content is the identity, so a rename keeps the job."""
    a = media(tmp_path, "a.mp4", b"same bytes")
    b = media(tmp_path, "renamed.mp4", b"same bytes")
    assert source_fingerprint(a) == source_fingerprint(b)


def test_the_hash_is_memoised_within_a_process(tmp_path):
    """A submit hashes once; retry, plan and the job id reuse that result."""
    import dubforge.jobs as jobs_module

    jobs_module._SOURCE_HASHES.clear()
    path = media(tmp_path)
    first = source_fingerprint(path)
    cached = len(jobs_module._SOURCE_HASHES)
    assert cached == 1
    assert source_fingerprint(path) == first
    assert len(jobs_module._SOURCE_HASHES) == cached, "a second call must not re-read the file"


def test_a_missing_source_is_a_clear_error(tmp_path):
    with pytest.raises(JobError, match="cannot fingerprint"):
        source_fingerprint(tmp_path / "absent.mp4")


# ------------------------------------------------------------ request identity


def test_different_target_languages_are_different_requests():
    assert request_fingerprint(Settings(target_lang="hi")) != request_fingerprint(
        Settings(target_lang="ta")
    )


def test_retranslate_is_part_of_the_identity():
    """It decides whether supplied translations are kept or replaced."""
    on = request_fingerprint(Settings(retranslate=True))
    off = request_fingerprint(Settings(retranslate=False))
    assert on != off


def test_lipsync_and_separator_are_part_of_the_identity():
    base = Settings()
    assert request_fingerprint(base) != request_fingerprint(Settings(lipsync="synclabs"))
    assert request_fingerprint(base) != request_fingerprint(Settings(separator="demucs"))


def test_a_supplied_transcript_is_identified_by_content(tmp_path):
    """Same path, changed bytes: a different request."""
    path = tmp_path / "t.json"
    path.write_text('{"segments": []}', encoding="utf-8")
    first = request_fingerprint(Settings(transcript=str(path)))
    path.write_text('{"segments": [{"index": 0}]}', encoding="utf-8")
    second = request_fingerprint(Settings(transcript=str(path)))
    assert first != second


def test_the_same_transcript_under_another_path_is_the_same_request(tmp_path):
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    for path in (one, two):
        path.write_text('{"segments": []}', encoding="utf-8")
    assert request_fingerprint(Settings(transcript=str(one))) == request_fingerprint(
        Settings(transcript=str(two))
    )


def test_a_missing_transcript_does_not_crash_the_identity(tmp_path):
    value = request_fingerprint(Settings(transcript=str(tmp_path / "absent.json")))
    assert isinstance(value, str) and len(value) == 12


def test_irrelevant_settings_stay_out_of_the_identity():
    """Limits, logging and paths do not change the artifact."""
    a = request_fingerprint(Settings(workdir="/one", cache=True, max_input_seconds=60))
    b = request_fingerprint(Settings(workdir="/two", cache=False, max_input_seconds=999))
    assert a == b


def test_the_transcript_hash_is_the_file_bytes(tmp_path):
    path = tmp_path / "t.json"
    path.write_bytes(b"abc")
    import hashlib

    assert file_content_hash(path) == hashlib.sha256(b"abc").hexdigest()


# ------------------------------------------------------------- through the store


def test_two_videos_of_equal_size_are_two_jobs(tmp_path):
    store = JobStore(tmp_path / "work")
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    a = media(tmp_path, "a.mp4", b"AAAA", mtime=1000)
    b = media(tmp_path, "b.mp4", b"BBBB", mtime=1000)
    first = store.create(a, settings)
    second = store.create(b, settings)
    assert first.id != second.id
    assert first.request_fingerprint != second.request_fingerprint


def test_identical_requests_still_deduplicate(tmp_path):
    store = JobStore(tmp_path / "work")
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    path = media(tmp_path)
    first = store.create(path, settings)
    again = store.create(path, settings)
    assert again.id == first.id and again.duplicate is True


def test_a_changed_transcript_produces_a_new_job(tmp_path):
    store = JobStore(tmp_path / "work")
    path = media(tmp_path)
    transcript = tmp_path / "t.json"
    transcript.write_text('{"segments": []}', encoding="utf-8")
    first = store.create(
        path, Settings(target_lang="hi", workdir=str(tmp_path / "work"), transcript=str(transcript))
    )
    transcript.write_text(
        '{"segments": [{"index": 0, "start": 0, "end": 1, "text": "x"}]}', encoding="utf-8"
    )
    second = store.create(
        path, Settings(target_lang="hi", workdir=str(tmp_path / "work"), transcript=str(transcript))
    )
    assert first.id != second.id


def test_a_legacy_two_part_job_still_loads(tmp_path):
    """Records written before request-addressed ids must remain readable."""
    import json
    import time

    from dubforge.jobs import new_job_id

    workdir = tmp_path / "work"
    store = JobStore(workdir)
    path = media(tmp_path)
    legacy_id = new_job_id(path)
    (workdir / "registry" / f"{legacy_id}.json").write_text(
        json.dumps(
            {
                "id": legacy_id,
                "source": str(path),
                "target_lang": "hi",
                "status": "queued",
                "stage": "queued",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    job = store.get(legacy_id)
    assert job is not None and job.legacy_id is True
