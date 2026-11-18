"""JobRunner.wait(): the caller's deadline is the contract.

0.9.0 made the poll loop tolerate a transient miss by string-matching "no job" and retrying a
fixed number of times. That treated the symptom: `JobStore.get()` returned `None` for a missing
record, an unreadable one and a corrupt one alike, so all three became "no job" — a lie about a
job that existed, and a silent hiding of data loss. These tests pin the classification and the
deadline, because those are the two things a polling caller depends on.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from dubforge.config import Settings
from dubforge.jobs import JobError, JobRunner, JobStore


def _source(tmp_path) -> Path:
    path = tmp_path / "lesson.mp4"
    path.write_bytes(b"x")
    return path


@pytest.fixture()
def job(tmp_path):
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    store = JobStore(settings.workdir)
    record = store.create(_source(tmp_path), settings)
    runner = JobRunner(settings)
    yield settings, store, record, runner
    runner.shutdown(wait=True)


def _elapsed(fn) -> tuple[float, Exception | None]:
    started = time.monotonic()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the caller inspects it
        return time.monotonic() - started, exc
    return time.monotonic() - started, None


# ------------------------------------------------------- Test A: the deadline


def test_a_timeout_is_respected_for_a_nonexistent_job(job):
    _, _, _, runner = job
    elapsed, error = _elapsed(lambda: runner.wait("does-not-exist", timeout=1.0))
    assert isinstance(error, JobError)
    assert error.kind == "missing"
    assert str(error) == "no job 'does-not-exist'", "the precise error must survive"
    assert elapsed < 2.0, f"wait() overran a 1 s timeout by taking {elapsed:.2f}s"
    assert elapsed >= 0.9, "it must actually wait for the timeout, not fail at once"


def test_a_very_short_timeout_is_not_overrun_by_a_sleep(job):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    elapsed, error = _elapsed(lambda: runner.wait(record.id, timeout=0.05))
    assert isinstance(error, JobError) and error.kind == "job"
    assert elapsed < 0.5, f"a 50 ms timeout took {elapsed:.2f}s — a sleep was not clamped"


def test_timeout_zero_checks_once_and_does_not_claim_the_job_is_missing(job):
    """The 0.9.0 bug: an existing, running job was reported as 'no job' at timeout=0."""
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    with pytest.raises(JobError) as caught:
        runner.wait(record.id, timeout=0)
    assert caught.value.kind == "job"
    assert "no job" not in str(caught.value)
    assert "did not finish" in str(caught.value)


def test_timeout_zero_returns_a_finished_job(job):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    store.update(record.id, status="completed", qc_ok=True, output="/tmp/x.mp4")
    assert runner.wait(record.id, timeout=0).status == "completed"


# ------------------------------------------------ Test B/C/E: transient misses


def test_b_a_transient_miss_is_retried_and_the_job_is_returned(job, monkeypatch):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    real_status = JobRunner.status
    calls = {"n": 0}

    def flaky(self, job_id):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise JobError(f"no job {job_id!r}", kind="missing")
        return real_status(self, job_id)

    monkeypatch.setattr(JobRunner, "status", flaky)
    store.update(record.id, status="processing", pid=1)
    threading.Timer(
        0.3, lambda: store.update(record.id, status="completed", qc_ok=True, output="/tmp/x.mp4")
    ).start()
    final = runner.wait(record.id, timeout=5)
    assert final.status == "completed"
    assert calls["n"] > 3, "wait() must have retried through the misses"


def test_c_persistent_misses_still_respect_the_timeout(job, monkeypatch):
    settings, store, record, runner = job

    def always_missing(self, job_id):
        raise JobError(f"no job {job_id!r}", kind="missing")

    monkeypatch.setattr(JobRunner, "status", always_missing)
    elapsed, error = _elapsed(lambda: runner.wait(record.id, timeout=0.6))
    assert isinstance(error, JobError)
    assert elapsed < 1.6, f"retries extended the deadline: {elapsed:.2f}s for a 0.6 s timeout"


def test_e_repeated_misses_do_not_reset_the_deadline(job, monkeypatch):
    """A deadline that is recomputed per attempt is not a deadline."""
    settings, store, record, runner = job
    calls = {"n": 0}

    def missing_then_never(self, job_id):
        calls["n"] += 1
        raise JobError(f"no job {job_id!r}", kind="missing")

    monkeypatch.setattr(JobRunner, "status", missing_then_never)
    elapsed, _ = _elapsed(lambda: runner.wait(record.id, timeout=0.4))
    assert elapsed < 1.4, f"{calls['n']} attempts took {elapsed:.2f}s for a 0.4 s budget"


# ------------------------------------------- Test D/F: real jobs and completions


def test_d_a_job_that_never_existed_raises_the_missing_error(job):
    _, _, _, runner = job
    with pytest.raises(JobError) as caught:
        runner.wait("never-created", timeout=0.3)
    assert caught.value.kind == "missing"
    assert str(caught.value) == "no job 'never-created'"


def test_f_a_job_that_completes_during_polling_is_returned(job):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    threading.Timer(
        0.2, lambda: store.update(record.id, status="completed", qc_ok=True, output="/tmp/out.mp4")
    ).start()
    final = runner.wait(record.id, timeout=5)
    assert final.status == "completed"
    assert final.qc_ok is True


def test_f_a_job_that_fails_during_polling_returns_its_failure(job):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    threading.Timer(
        0.2, lambda: store.update(record.id, status="failed", error="provider exploded")
    ).start()
    final = runner.wait(record.id, timeout=5)
    assert final.status == "failed"
    assert "exploded" in (final.error or "")


def test_f_a_job_that_is_cancelled_during_polling_returns_its_state(job):
    settings, store, record, runner = job
    store.update(record.id, status="processing", pid=1)
    threading.Timer(0.2, lambda: store.update(record.id, status="cancelled")).start()
    assert runner.wait(record.id, timeout=5).status == "cancelled"


# --------------------------------------------- Test G: errors that are not misses


def test_g_a_corrupt_record_is_not_a_transient_miss(job):
    settings, store, record, runner = job
    store._path(record.id).write_text("{not json", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(JobError) as caught:
        runner.wait(record.id, timeout=2.0)
    assert caught.value.kind == "corrupt"
    assert "unreadable record" in str(caught.value)
    assert time.monotonic() - started < 0.5, "a corrupt record must not be retried"


@pytest.mark.skipif(os.name == "nt", reason="chmod does not deny the owner on Windows")
def test_g_an_unreadable_record_is_not_a_transient_miss(job):
    settings, store, record, runner = job
    path = store._path(record.id)
    os.chmod(path, 0o000)
    try:
        with pytest.raises(JobError) as caught:
            runner.wait(record.id, timeout=2.0)
    finally:
        os.chmod(path, 0o644)
    assert caught.value.kind == "unreadable"
    assert "cannot read job" in str(caught.value)


def test_g_an_unexpected_error_propagates_unchanged(job, monkeypatch):
    settings, store, record, runner = job

    def boom(self, job_id):
        raise ValueError("something else entirely")

    monkeypatch.setattr(JobRunner, "status", boom)
    with pytest.raises(ValueError, match="something else"):
        runner.wait(record.id, timeout=2.0)


def test_g_a_job_error_that_is_not_a_miss_propagates_immediately(job, monkeypatch):
    settings, store, record, runner = job

    def owned(self, job_id):
        raise JobError(f"job {job_id} is owned by another worker", kind="job")

    monkeypatch.setattr(JobRunner, "status", owned)
    started = time.monotonic()
    with pytest.raises(JobError, match="owned by another worker"):
        runner.wait(record.id, timeout=2.0)
    assert time.monotonic() - started < 0.5


# ------------------------------------- Test H: the store's own classification


def test_h_get_is_lenient_by_default_and_strict_on_request(job):
    settings, store, record, _ = job
    store._path(record.id).write_text("{not json", encoding="utf-8")
    assert store.get(record.id) is None, "the lenient contract is unchanged"
    with pytest.raises(JobError) as caught:
        store.get(record.id, strict=True)
    assert caught.value.kind == "corrupt"


def test_h_an_absent_record_is_none_in_both_modes(job):
    """Absence is a legitimate answer, not a failure: `strict` classifies *errors*, not gaps."""
    _, store, _, _ = job
    assert store.get("absent") is None
    assert store.get("absent", strict=True) is None


def test_h_joberror_keeps_its_old_shape(job):
    """Existing callers catch JobError and read the message; both still work."""
    error = JobError("no job 'x'")
    assert isinstance(error, RuntimeError)
    assert error.kind == "job", "the default kind keeps old construction sites meaningful"
    assert str(error) == "no job 'x'"


# ---------------------------------------- Phase 4: is the registry atomic?


def test_a_reader_never_sees_a_half_written_record(tmp_path):
    """A concurrent writer replaces the record 200 times; every read is a job or a clean miss."""
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    store = JobStore(settings.workdir)
    record = store.create(_source(tmp_path), settings)
    stop = threading.Event()
    failures: list[str] = []

    def writer():
        while not stop.is_set():
            store.update(record.id, qc_ok=None)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            try:
                job = store.get(record.id, strict=True)
            except JobError as exc:
                failures.append(f"{exc.kind}: {exc}")
                continue
            assert job is not None
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not failures, f"a read hit a half-written record: {failures[:3]}"
