"""Job lifecycle: the state machine, its persistence, and its refusal to lie."""

import json
import os
import time

import pytest

from dubforge.config import Settings
from dubforge.jobs import (
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    PROCESSING,
    QUEUED,
    Job,
    JobError,
    JobStore,
    new_job_id,
)


@pytest.fixture()
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "work")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(target_lang="hi", workdir=str(tmp_path / "work"))


def _source(tmp_path, name="lesson.mp4", data=b"x"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------- identity


def test_job_id_is_derived_from_the_source_path(tmp_path):
    a = _source(tmp_path / "one" / "lesson.mp4")
    b = _source(tmp_path / "two" / "lesson.mp4")
    assert new_job_id(a) == new_job_id(a)
    assert new_job_id(a) != new_job_id(b)
    assert new_job_id(a).startswith("lesson-")


def test_create_is_idempotent_for_the_same_source(store, settings, tmp_path):
    source = _source(tmp_path)
    first = store.create(source, settings)
    second = store.create(source, settings)
    assert first.id == second.id
    assert second.duplicate is True
    assert len(list((tmp_path / "work" / "registry").glob("*.json"))) == 1


def test_invalid_job_ids_never_reach_the_filesystem(store):
    for bad in ("../../etc/passwd", "with space", "", "x" * 100, "sneaky/../up"):
        with pytest.raises(JobError):
            store.get(bad)


# -------------------------------------------------------------------- lifecycle


def test_full_lifecycle_transitions(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert job.status == QUEUED

    for status in ("validating", PROCESSING, "quality_check"):
        job = store.update(job.id, status=status, stage=status)
        assert store.get(job.id).status == status

    job = store.update(job.id, status=COMPLETED, qc_ok=True, finished_at=time.time())
    stored = store.get(job.id)
    assert stored.status == COMPLETED
    assert stored.terminal is True
    assert stored.qc_ok is True


def test_state_is_persisted_atomically(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, stage="synthesise", progress=0.4)
    path = tmp_path / "work" / "registry" / f"{job.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == PROCESSING
    assert payload["progress"] == 0.4
    assert not list(path.parent.glob("*.tmp")), "atomic writes must not leave temp files"


def test_a_corrupt_job_file_is_a_miss_not_a_crash(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    (tmp_path / "work" / "registry" / f"{job.id}.json").write_text("{not json", encoding="utf-8")
    assert store.get(job.id) is None


# --------------------------------------------------------------------- recovery


def test_a_job_left_by_a_dead_process_becomes_interrupted(store, settings, tmp_path):
    """Never reported as completed: that is the one lie a job store must not tell."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(
        job.id, status=PROCESSING, stage="synthesise", pid=999_999_999, started_at=time.time() - 30
    )
    recovered = store.get(job.id)
    assert recovered.status == INTERRUPTED
    assert recovered.error_kind == "interrupted"
    assert recovered.terminal


def test_our_own_running_job_is_not_declared_interrupted(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid(), started_at=time.time())
    assert store.get(job.id).status == PROCESSING


def test_a_queued_job_is_not_interrupted(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.get(job.id).status == QUEUED


def test_recover_lists_what_it_fixed(store, settings, tmp_path):
    jobs = []
    for name in ("a.mp4", "b.mp4"):
        job = store.create(_source(tmp_path, name), settings)
        store.update(job.id, status=PROCESSING, pid=999_999_999)
        jobs.append(job.id)
    recovered = store.recover()
    assert {job.id for job in recovered} == set(jobs)


# ------------------------------------------------------------------ cancellation


def test_cancel_marks_a_queued_job_immediately(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    cancelled = store.request_cancel(job.id)
    assert cancelled.status == CANCELLED
    assert store.is_cancelled(job.id)


def test_cancel_asks_a_running_job_politely(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid())
    asked = store.request_cancel(job.id)
    assert asked.status == PROCESSING  # still running; the pipeline checks the flag
    assert asked.cancel_requested is True
    assert store.is_cancelled(job.id) is True


def test_cancel_is_idempotent_on_a_finished_job(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=COMPLETED, qc_ok=True)
    again = store.request_cancel(job.id)
    assert again.status == COMPLETED


# ----------------------------------------------------------------------- retry


def test_retry_resets_a_failed_job_within_budget(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=FAILED, error="boom", error_kind="server")
    retried = store.retry(job.id, max_retries=2)
    assert retried.status == QUEUED
    assert retried.attempts == 1
    assert retried.error == ""


def test_retry_refuses_a_running_job(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid())
    with pytest.raises(JobError):
        store.retry(job.id)


def test_retry_budget_is_enforced(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=FAILED, attempts=3)
    with pytest.raises(JobError) as exc:
        store.retry(job.id, max_retries=2)
    assert "attempts" in str(exc.value)


# ---------------------------------------------------------------- list/paginate


def test_list_is_newest_first_and_paginates(store, settings, tmp_path):
    for index in range(5):
        job = store.create(_source(tmp_path, f"lesson{index}.mp4"), settings)
        job.created_at = 1_700_000_000 + index
        store.save(job)

    page, total = store.list(limit=2, offset=0)
    assert total == 5
    assert len(page) == 2
    assert page[0].created_at > page[1].created_at
    page2, _ = store.list(limit=2, offset=2)
    assert {j.id for j in page} & {j.id for j in page2} == set()


def test_list_filters_by_status(store, settings, tmp_path):
    done = store.create(_source(tmp_path, "a.mp4"), settings)
    store.update(done.id, status=COMPLETED, qc_ok=True)
    store.create(_source(tmp_path, "b.mp4"), settings)
    completed, total = store.list(status=COMPLETED)
    assert total == 1 and completed[0].id == done.id


def test_summary_carries_no_transcript_and_no_paths_to_secrets(tmp_path, settings):
    store = JobStore(tmp_path / "work")
    job = store.create(_source(tmp_path), settings)
    summary = job.summary()
    assert "transcript" not in json.dumps(summary).lower()
    assert summary["job_id"] == job.id


# --------------------------------------------------------------------- cleanup


def test_cleanup_only_touches_finished_jobs(store, settings, tmp_path):
    finished = store.create(_source(tmp_path, "done.mp4"), settings)
    store.update(finished.id, status=COMPLETED, qc_ok=True, finished_at=time.time() - 86400 * 5)
    running = store.create(_source(tmp_path, "running.mp4"), settings)
    store.update(running.id, status=PROCESSING, pid=os.getpid())

    result = store.cleanup(older_than_days=1, dry_run=False)
    assert finished.id in result["removed"]
    assert running.id not in result["removed"]
    assert store.get(running.id) is not None


def test_cleanup_dry_run_deletes_nothing(store, settings, tmp_path):
    job = store.create(_source(tmp_path), settings)
    store.update(job.id, status=COMPLETED, qc_ok=True)
    result = store.cleanup(dry_run=True)
    assert job.id in result["removed"]
    assert store.get(job.id) is not None


def test_cleanup_removes_the_job_directory_but_not_anything_outside(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=COMPLETED, qc_ok=True)
    job_dir = tmp_path / "work" / "jobs" / job.id
    job_dir.mkdir(parents=True)
    (job_dir / "dub.wav").write_bytes(b"x")
    precious = tmp_path / "precious.txt"
    precious.write_text("keep me", encoding="utf-8")

    store.cleanup(dry_run=False)
    assert not job_dir.exists()
    assert precious.exists()
    assert store.get(job.id) is None


def test_job_dataclass_round_trips_through_json():
    job = Job(id="a-1234", source="/x/y.mp4", target_lang="hi", providers={"tts": "sarvam"})
    again = Job.from_dict(json.loads(json.dumps(job.to_dict())))
    assert again.id == job.id and again.providers == {"tts": "sarvam"}
    assert again.from_dict({**job.to_dict(), "unknown_field": 1}).id == job.id
