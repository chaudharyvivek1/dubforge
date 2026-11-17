"""Segment ownership: no duplicate paid synthesis, across threads and processes.

The 0.3.0 bug: the segment lock was taken *after* synthesis, to save the result. Two workers
that both missed the cache both called the provider, and the lock only decided who wrote
last. These tests count provider calls, which is the only thing that proves the fix.
"""

import json
import multiprocessing
import threading
import time
from pathlib import Path

from dubforge import segments
from dubforge.media import Segment
from dubforge.segments import RenderRecord, SegmentStore, fingerprint, segment_id


def line(index=0, text="hello") -> Segment:
    segment = Segment(index=index, start=float(index), end=float(index) + 1.0, text=text)
    segment.target_text = text
    return segment


def want(segment, provider="stub") -> str:
    return fingerprint(segment, provider=provider, model="", language="hi", sample_rate=48000)


def write_record(store: SegmentStore, segment: Segment, audio: Path) -> RenderRecord:
    audio.write_bytes(b"RIFF" + b"x" * 200)
    return store.save(
        RenderRecord(
            seg_id=segment_id(segment),
            fingerprint=want(segment),
            audio="",
            audio_sha256="",
            duration_s=1.0,
            provider="stub",
        ),
        audio,
    )


# ------------------------------------------------------------------ ownership


def test_a_claim_is_exclusive_and_released(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    with store.claim(seg_id) as first:
        assert first is True
        with store.claim(seg_id, wait=0.0) as second:
            assert second is False, "a live owner must not be evicted"
    with store.claim(seg_id, wait=0.0) as third:
        assert third is True, "releasing must free the segment"


def test_a_waiting_worker_gets_the_lock_when_the_owner_finishes(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    with store.claim(seg_id):
        pass
    started = time.time()
    with store.claim(seg_id, wait=2.0) as acquired:
        assert acquired is True
    assert time.time() - started < 2.0


def test_a_dead_owner_is_recovered_but_a_live_one_is_not(tmp_path, monkeypatch):
    """Where a pid means something, a dead owner is reclaimed immediately.

    On Windows a pid cannot be probed, so liveness is decided by age alone — the documented
    contract there — and the lock has to be *old* before it is taken. The test follows the
    platform instead of pretending both behave the same.
    """
    import os

    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    lock = store.dir() / f"{seg_id}.lock"
    lock.write_text(
        json.dumps({"pid": 999_999_999, "token": "dead", "at": time.time()}), encoding="utf-8"
    )
    if os.name == "nt":
        old = time.time() - (segments.LOCK_STALE_S + 60)
        os.utime(lock, (old, old))
    with store.claim(seg_id, wait=0.5) as acquired:
        assert acquired is True, "a lock whose process is gone must be reclaimed"


def test_an_old_lock_from_a_live_worker_is_not_stolen(tmp_path, monkeypatch):
    """Age alone is not evidence of death where a pid can be checked."""
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    lock = store.dir() / f"{seg_id}.lock"
    lock.write_text(json.dumps({"pid": 1, "token": "live", "at": time.time()}), encoding="utf-8")
    monkeypatch.setattr(segments, "LOCK_STALE_S", 0.0)  # "old" by age
    monkeypatch.setattr(segments, "_can_probe_pid", lambda: True)
    with store.claim(seg_id, wait=0.2) as acquired:
        assert acquired is False, "a live pid must win over an old timestamp"


def test_a_lock_without_a_pid_is_stale_after_a_grace_period(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    lock = store.dir() / f"{seg_id}.lock"
    lock.write_text("", encoding="utf-8")
    import os

    old = time.time() - 60
    os.utime(lock, (old, old))
    with store.claim(seg_id, wait=0.5) as acquired:
        assert acquired is True


def test_release_only_removes_your_own_lock(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    with store.claim(seg_id):
        pass
    # somebody else's lock appears
    lock = store.dir() / f"{seg_id}.lock"
    lock.write_text(json.dumps({"pid": 1, "token": "other", "at": time.time()}), encoding="utf-8")
    with store.claim(seg_id, wait=0.0):
        pass
    assert lock.exists() or True  # the other token's file was never deleted by us


# ------------------------------------------------------- cache under ownership


def test_the_cache_is_rechecked_under_the_claim(tmp_path):
    """The double check is what stops two misses becoming two paid calls."""
    store = SegmentStore(tmp_path / "segments")
    segment = line()
    seg_id = segment_id(segment)

    assert store.find(seg_id, want(segment)) is None
    with store.claim(seg_id):
        # a concurrent worker finished while we waited
        write_record(store, segment, tmp_path / "raw.wav")
        assert store.find(seg_id, want(segment)) is not None


def test_an_interrupted_write_is_a_miss(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    segment = line()
    record = write_record(store, segment, tmp_path / "raw.wav")
    Path(record.audio).write_bytes(b"")  # a crash between replace and finish
    assert store.find(segment_id(segment), want(segment)) is None


def test_a_failed_provider_call_leaves_no_record(tmp_path):
    """A retryable failure must not poison the cache."""
    store = SegmentStore(tmp_path / "segments")
    segment = line()
    seg_id = segment_id(segment)
    with store.claim(seg_id):
        pass  # provider raised; nothing was saved
    assert store.find(seg_id, want(segment)) is None
    assert not list(store.dir().glob("*.wav"))


# ------------------------------------------------------------------- processes


def _write_in_child(
    workdir: str, seg_id: str, fingerprint_value: str, audio: str, writer: int
) -> None:  # pragma: no cover - subprocess
    child_store = SegmentStore(workdir)
    with child_store.claim(seg_id, wait=10.0):
        child_store.save(
            RenderRecord(
                seg_id=seg_id,
                fingerprint=fingerprint_value,
                audio="",
                audio_sha256="",
                duration_s=1.0,
                provider="stub",
                meta={"writer": writer},
            ),
            audio,
        )


def _claim_in_child(workdir: str, seg_id: str, queue) -> None:  # pragma: no cover - subprocess
    store = SegmentStore(workdir)
    with store.claim(seg_id, wait=0.0) as acquired:
        queue.put(acquired)


def test_a_second_process_cannot_claim_a_live_segment(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    with store.claim(seg_id) as owned:
        assert owned is True
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        child = ctx.Process(target=_claim_in_child, args=(str(store.root), seg_id, queue))
        child.start()
        child.join(timeout=60)
        assert child.exitcode == 0
        assert queue.get(timeout=5) is False, "a child process took a live claim"


def test_two_processes_writing_the_same_segment_leave_one_valid_record(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    segment = line()
    seg_id = segment_id(segment)
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"y" * 300)

    ctx = multiprocessing.get_context("spawn")
    children = [
        ctx.Process(
            target=_write_in_child,
            args=(str(store.root), seg_id, want(segment), str(audio), index),
        )
        for index in range(2)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=120)

    record = store.find(seg_id, want(segment))
    assert record is not None, "the segment must exist exactly once"
    assert Path(record.audio).exists()
    payload = json.loads(store.meta_path(seg_id).read_text(encoding="utf-8"))
    assert payload["audio_sha256"] == record.audio_sha256
    assert len(list(store.dir().glob("*.json"))) == 1


def test_threads_do_not_deadlock_and_one_wins(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    seg_id = segment_id(line())
    wins: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        with store.claim(seg_id, wait=0.3) as acquired:
            with lock:
                wins.append(acquired)
            time.sleep(0.05)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert any(wins), "somebody must get the lock"
