"""Cache correctness: a wrong cache key is a wrong dub, delivered confidently."""

import json
import os
import threading
import time

import pytest

from dubforge.media import Segment, Transcript, content_hash, file_fingerprint
from dubforge.pipeline import _load_cached, _save_cached


def _write(path, data: bytes, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _transcript(text: str = "hello") -> Transcript:
    return Transcript(segments=[Segment(index=0, start=0.0, end=1.0, text=text)], language="en")


# ------------------------------------------------------------------ fingerprints


def test_same_name_and_size_but_different_content_never_collide(tmp_path):
    """The original bug: two different videos with one name, one size, one mtime."""
    a = _write(tmp_path / "a" / "lesson.mp4", b"AAAA", mtime=1_700_000_000)
    b = _write(tmp_path / "b" / "lesson.mp4", b"BBBB", mtime=1_700_000_000)
    assert a.stat().st_size == b.stat().st_size
    assert content_hash(a) != content_hash(b)
    assert file_fingerprint(a) != file_fingerprint(b)


def test_same_content_with_a_new_mtime_reuses_the_entry(tmp_path):
    """Re-saving an identical file must not pay for recognition twice."""
    a = _write(tmp_path / "lesson.mp4", b"same bytes", mtime=1_600_000_000)
    first = file_fingerprint(a)
    os.utime(a, (1_700_000_000, 1_700_000_000))
    assert file_fingerprint(a) == first


def test_moving_or_renaming_a_file_keeps_the_key(tmp_path):
    a = _write(tmp_path / "lesson.mp4", b"bytes")
    first = file_fingerprint(a)
    moved = tmp_path / "other" / "renamed.mp4"
    moved.parent.mkdir()
    a.rename(moved)
    assert file_fingerprint(moved) == first


def test_asr_configuration_is_part_of_the_key(tmp_path):
    a = _write(tmp_path / "lesson.mp4", b"bytes")
    small = file_fingerprint(a, "asr=faster_whisper:small")
    large = file_fingerprint(a, "asr=faster_whisper:large-v3")
    other_provider = file_fingerprint(a, "asr=deepgram:")
    assert len({small, large, other_provider}) == 3


def test_content_hash_streams_large_files(tmp_path):
    """A file bigger than one chunk must hash the whole thing, not the first block."""
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:
        handle.write(b"a" * (1 << 20))
        handle.write(b"b" * (1 << 20))
    assert content_hash(big) != content_hash(_write(tmp_path / "half.bin", b"a" * (1 << 20)))


# ------------------------------------------------------------------------ cache


def test_roundtrip_and_fingerprint_mismatch(tmp_path):
    cache = tmp_path / "cache"
    _save_cached(cache, "key-1", _transcript("namaste"))
    loaded = _load_cached(cache, "key-1")
    assert loaded is not None
    assert loaded.segments[0].text == "namaste"
    assert _load_cached(cache, "key-2") is None


def test_corrupt_cache_is_ignored_not_fatal(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "transcript.json").write_text("{not json", encoding="utf-8")
    assert _load_cached(cache, "anything") is None
    # and it can be repaired by the next write
    _save_cached(cache, "key-1", _transcript())
    assert _load_cached(cache, "key-1") is not None


def test_cache_with_a_malformed_payload_is_ignored(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "transcript.json").write_text(
        json.dumps({"fingerprint": "k", "transcript": {"segments": "not-a-list"}}), encoding="utf-8"
    )
    assert _load_cached(cache, "k") is None


def test_cache_from_an_older_version_without_segments_is_ignored(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "transcript.json").write_text(
        json.dumps({"fingerprint": "k", "transcript": {"language": "en", "segments": []}}),
        encoding="utf-8",
    )
    assert _load_cached(cache, "k") is None


def test_concurrent_writers_never_corrupt_the_cache(tmp_path):
    """Eight processes' worth of writers, one file, always valid JSON at the end."""
    cache = tmp_path / "cache"
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for _ in range(5):
                _save_cached(cache, f"key-{n}", _transcript(f"line from {n}"))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    payload = json.loads((cache / "transcript.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload["fingerprint"].startswith("key-")
    assert _load_cached(cache, payload["fingerprint"]) is not None
    assert not (cache / ".lock").exists(), "the cache lock must be released"


# --------------------------------------------------------------- cache locking


def test_a_live_lock_blocks_the_write_instead_of_writing_unlocked(tmp_path):
    """Silently writing without the lock is how two writers corrupt one cache file."""
    from dubforge.pipeline import CacheWriteError, _save_cached

    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / ".lock").write_text(
        json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8"
    )

    with pytest.raises(CacheWriteError) as exc:
        _save_cached(cache, "key", _transcript(), timeout=0.3)
    assert "cache lock" in str(exc.value)
    assert not (cache / "transcript.json").exists(), "nothing may be written while the lock is held"


def test_a_stale_lock_is_recovered(tmp_path):
    from dubforge.pipeline import _save_cached

    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    lock = cache / ".lock"
    lock.write_text(json.dumps({"pid": 999_999_999, "at": time.time()}), encoding="utf-8")
    if os.name == "nt":  # liveness is judged by age there
        old = time.time() - 600
        os.utime(lock, (old, old))

    _save_cached(cache, "key", _transcript("recovered"), timeout=2.0)
    assert _load_cached(cache, "key").segments[0].text == "recovered"
    assert not lock.exists()


def test_a_dead_process_lock_is_recovered_on_posix(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows has no safe pid probe; age-based recovery is tested above")
    from dubforge.pipeline import _save_cached

    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / ".lock").write_text(
        json.dumps({"pid": 999_999_999, "at": time.time()}), encoding="utf-8"
    )
    _save_cached(cache, "key", _transcript(), timeout=2.0)
    assert _load_cached(cache, "key") is not None


def test_an_unwritable_cache_directory_raises_a_clear_error(tmp_path):
    from dubforge.pipeline import CacheWriteError, _save_cached

    blocker = tmp_path / "cache"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    with pytest.raises((CacheWriteError, OSError)) as exc:
        _save_cached(blocker, "key", _transcript(), timeout=0.2)
    assert "cache" in str(exc.value).lower()


def test_a_thread_holding_the_cache_lock_blocks_other_threads_without_hanging(tmp_path):
    """Two threads in one process share a pid, so the file lock cannot tell them apart."""

    from dubforge.pipeline import CacheWriteError, _save_cached, _thread_lock_for

    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    held = _thread_lock_for(cache)
    held.acquire()
    try:
        with pytest.raises(CacheWriteError) as exc:
            _save_cached(cache, "key", _transcript(), timeout=0.3)
        assert "thread" in str(exc.value)
        assert not (cache / "transcript.json").exists()
    finally:
        held.release()

    # once released, the same call succeeds
    _save_cached(cache, "key", _transcript(), timeout=2.0)
    assert _load_cached(cache, "key") is not None


def test_concurrent_threads_serialise_instead_of_failing(tmp_path):
    import threading

    from dubforge.pipeline import _save_cached

    cache = tmp_path / "cache"
    results: list[str] = []
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            results.append(_save_cached(cache, f"k{n}", _transcript(f"thread {n}"), timeout=5.0))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(results) == 6
    assert not (cache / ".lock").exists()
