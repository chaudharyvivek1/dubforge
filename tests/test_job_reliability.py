"""Job reliability: heartbeat, recovery, identity, claims, attempts.

The bugs these exist for, all confirmed against 0.2.0:

* a live process whose provider call outran the heartbeat window was reported
  `interrupted` — a healthy dub killed by a status request;
* one source could only ever have one job, so Hindi and Tamil could not run together;
* ownership lived in an in-memory set, so two runners could both run one request;
* a retry left the previous attempt's output fields in place.
"""

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

import dubforge.jobs as jobs_module
import dubforge.pipeline as pipeline_module
from dubforge.config import Settings
from dubforge.jobs import (
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    PROCESSING,
    QUEUED,
    JobError,
    JobRunner,
    JobStore,
    new_job_id,
    request_fingerprint,
    source_fingerprint,
)

STALE_S = 180.0


@pytest.fixture()
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "work")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(target_lang="hi", workdir=str(tmp_path / "work"))


def _source(tmp_path, name="lesson.mp4", data=b"x") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ heartbeat


@pytest.mark.skipif(not jobs_module._can_probe_pid(), reason="a pid is not evidence here")
def test_a_live_process_is_not_interrupted_by_a_stale_heartbeat(store, settings, tmp_path):
    """The 0.2.0 bug: a four-minute provider call looked like a dead worker."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(
        job.id,
        status=PROCESSING,
        stage="synthesise",
        pid=os.getpid(),
        heartbeat_at=time.time() - (STALE_S + 300),
    )
    assert store.get(job.id).status == PROCESSING


def test_a_fresh_heartbeat_keeps_a_job_alive_where_pids_cannot_be_probed(
    store, settings, tmp_path, monkeypatch
):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid(), heartbeat_at=time.time())
    monkeypatch.setattr(jobs_module, "_can_probe_pid", lambda: False)
    assert store.get(job.id).status == PROCESSING


def test_a_stale_heartbeat_interrupts_a_job_where_pids_cannot_be_probed(
    store, settings, tmp_path, monkeypatch
):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(
        job.id, status=PROCESSING, pid=os.getpid(), heartbeat_at=time.time() - (STALE_S + 60)
    )
    monkeypatch.setattr(jobs_module, "_can_probe_pid", lambda: False)
    assert store.get(job.id).status == INTERRUPTED


@pytest.mark.skipif(not jobs_module._can_probe_pid(), reason="pids cannot be probed here")
def test_a_dead_process_is_interrupted_even_with_a_fresh_heartbeat(store, settings, tmp_path):
    """The heartbeat cannot outlive the process: a dead pid wins."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=999_999_999, heartbeat_at=time.time())
    assert store.get(job.id).status == INTERRUPTED


def test_a_clock_that_runs_ahead_is_not_death(store, settings, tmp_path):
    """A heartbeat from the future (clock skew between hosts) counts as fresh."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid(), heartbeat_at=time.time() + 600)
    assert store.get(job.id).status == PROCESSING


def test_the_stale_threshold_is_configurable(store, settings, tmp_path, monkeypatch):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=os.getpid(), heartbeat_at=time.time() - 60)
    monkeypatch.setattr(jobs_module, "_can_probe_pid", lambda: False)
    assert store.get(job.id).status == PROCESSING  # 60s < 180s default
    monkeypatch.setenv("DUBFORGE_JOB_HEARTBEAT_STALE_S", "30")
    assert store.get(job.id).status == INTERRUPTED


def test_a_died_worker_is_recovered_after_a_restart(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(
        job.id,
        status=PROCESSING,
        stage="transcribe",
        pid=999_999_999,
        heartbeat_at=time.time() - 600,
        started_at=time.time() - 600,
    )

    fresh_store = JobStore(tmp_path / "work")  # a new process, reading the same files
    recovered = fresh_store.recover()
    assert [item.id for item in recovered] == [job.id]
    assert fresh_store.get(job.id).status == INTERRUPTED
    assert fresh_store.get(job.id).status != COMPLETED


def test_two_readers_agree_and_leave_valid_json(store, settings, tmp_path):
    """Two processes reading a dead job must not corrupt the document."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=999_999_999, heartbeat_at=time.time() - 600)
    results: list[str] = []

    def read() -> None:
        results.append(JobStore(tmp_path / "work").get(job.id).status)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [INTERRUPTED] * 4
    payload = json.loads((tmp_path / "work" / "registry" / f"{job.id}.json").read_text())
    assert payload["status"] == INTERRUPTED


# ------------------------------------------------------------------- identity


def test_one_source_two_languages_are_two_jobs(store, tmp_path):
    source = _source(tmp_path)
    hi = store.create(source, Settings(target_lang="hi", workdir=str(tmp_path / "work")))
    ta = store.create(source, Settings(target_lang="ta", workdir=str(tmp_path / "work")))
    assert hi.id != ta.id
    assert hi.status == QUEUED and ta.status == QUEUED


def test_the_same_request_deduplicates(store, tmp_path):
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    first = store.create(source, settings)
    again = store.create(source, settings)
    assert again.id == first.id and again.duplicate is True


def test_different_providers_are_different_jobs(store, tmp_path):
    source = _source(tmp_path)
    base = {"target_lang": "hi", "workdir": str(tmp_path / "work")}
    a = store.create(source, Settings(tts="stub", **base))
    b = store.create(source, Settings(tts="edge_tts", **base))
    assert a.id != b.id


def test_voices_and_glossary_change_the_request_hash(store, tmp_path):
    source = _source(tmp_path)
    base = {"target_lang": "hi", "workdir": str(tmp_path / "work")}
    plain = store.create(source, Settings(**base))
    voiced = store.create(source, Settings(voice_pool=["a"], **base))
    glossary = store.create(source, Settings(glossary={"ISRO": "इसरो"}, **base))
    assert len({plain.id, voiced.id, glossary.id}) == 3


def test_irrelevant_settings_do_not_change_the_request_hash():
    one = request_fingerprint(Settings(target_lang="hi", workdir="/a", cache=True))
    two = request_fingerprint(Settings(target_lang="hi", workdir="/b", cache=False))
    assert one == two  # workdir and cache do not change the result


def test_editing_the_source_changes_the_request_hash(tmp_path):
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "w"))
    before = request_fingerprint(settings, source_fingerprint(source))
    _source(tmp_path, data=b"different bytes")
    after = request_fingerprint(settings, source_fingerprint(source))
    assert before != after


def test_legacy_two_part_ids_are_still_readable(store, settings, tmp_path):
    source = _source(tmp_path)
    legacy_id = new_job_id(source)  # no request hash: the 0.2.0 shape
    payload = {
        "id": legacy_id,
        "source": str(source),
        "target_lang": "hi",
        "status": QUEUED,
        "stage": QUEUED,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    (tmp_path / "work" / "registry" / f"{legacy_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    job = store.get(legacy_id)
    assert job is not None and job.legacy_id is True
    with pytest.raises(JobError):
        store.settings_for(legacy_id)  # no request snapshot to retry from


# --------------------------------------------------------------------- claims


def test_only_one_worker_can_claim_a_job(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.claim(job.id, "token-a") is True
    assert store.claim(job.id, "token-b") is False
    assert store.owns(job.id, "token-a") is True
    assert store.owns(job.id, "token-b") is False


def test_a_dead_claim_is_reclaimed(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    (tmp_path / "work" / "registry" / f"{job.id}.claim").write_text(
        json.dumps(
            {
                "token": "dead",
                "pid": 999_999_999,
                "host": jobs_module.socket.gethostname(),
                "since": time.time() - 600,
            }
        ),
        encoding="utf-8",
    )
    assert store.claim(job.id, "fresh") is True
    assert store.owns(job.id, "fresh")


def test_a_stale_worker_cannot_update_a_newer_attempt(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.claim(job.id, "owner")
    with pytest.raises(JobError) as exc:
        store.update(job.id, token="someone-else", status=PROCESSING)
    assert "owned by another worker" in str(exc.value)


def test_heartbeat_only_from_the_owner(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.claim(job.id, "owner")
    store.update(job.id, token="owner", status=PROCESSING, heartbeat_at=0)
    assert store.heartbeat(job.id, "not-the-owner") is False
    assert store.heartbeat(job.id, "owner") is True
    assert (store.get(job.id, recover=False).heartbeat_at or 0) > time.time() - 60


def test_release_lets_another_worker_take_over(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.claim(job.id, "first")
    store.release(job.id, "first")
    assert store.claim(job.id, "second") is True


def test_claim_is_visible_to_another_store_instance(store, settings, tmp_path):
    """Cross-process in effect: the claim is a file, not a set in memory."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.claim(job.id, "owner")
    other = JobStore(tmp_path / "work")
    assert other.claim(job.id, "thief") is False


def _try_claim_in_subprocess(
    workdir: str, job_id: str, queue
) -> None:  # pragma: no cover - subprocess
    store = JobStore(workdir)
    queue.put(store.claim(job_id, "child"))


def test_a_second_process_cannot_claim_a_live_job(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    assert store.claim(job.id, "parent")

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_try_claim_in_subprocess, args=(str(tmp_path / "work"), job.id, queue)
    )
    process.start()
    process.join(timeout=60)
    assert process.exitcode == 0
    assert queue.get(timeout=5) is False


# ------------------------------------------------------------------- attempts


def test_an_illegal_transition_is_refused(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    with pytest.raises(JobError) as exc:
        store.update(job.id, status=COMPLETED)
    assert "illegal transition" in str(exc.value)


def test_a_terminal_job_does_not_become_active_without_a_retry(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=1)
    store.update(job.id, status=FAILED, error="boom")
    with pytest.raises(JobError):
        store.update(job.id, status=PROCESSING)


def test_completed_with_failed_qc_is_distinguishable(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=1)
    store.update(job.id, status=COMPLETED, qc_ok=False, qc={"failures": ["translated"]})
    stored = store.get(job.id)
    assert stored.status == COMPLETED
    assert stored.qc_failed is True
    assert stored.summary()["qc_failed"] is True


def test_retry_clears_the_previous_attempts_results(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(
        job.id,
        status=PROCESSING,
        pid=1,
        output="/tmp/old.mp4",
        qc_ok=True,
        qc={"ok": True},
        warnings=["old"],
        providers_used={"tts": "stub"},
    )
    store.update(job.id, status=FAILED, error="boom", error_kind="server")
    retried = store.retry(job.id, max_retries=2)
    assert retried.status == QUEUED
    assert retried.output == ""
    assert retried.qc == {} and retried.qc_ok is None
    assert retried.warnings == []
    assert retried.providers_used == {}
    assert retried.attempts == 1
    assert any(entry.get("event") == "retry_requested" for entry in retried.attempt_history)


def test_retry_preserves_the_original_request(store, tmp_path):
    source = _source(tmp_path)
    settings = Settings(
        target_lang="ta",
        tts="stub",
        voice_pool=["v1"],
        glossary={"a": "b"},
        workdir=str(tmp_path / "work"),
    )
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=1)
    store.update(job.id, status=FAILED, error="boom")
    rebuilt = store.settings_for(job.id)
    assert rebuilt.target_lang == "ta"
    assert rebuilt.voice_pool == ["v1"]
    assert rebuilt.glossary == {"a": "b"}


def test_attempt_history_is_bounded(store, settings, tmp_path):
    source = _source(tmp_path)
    job = store.create(source, settings)
    job.attempt_history = [{"attempt": i} for i in range(80)]
    store.save(job)
    store.update(job.id, status=PROCESSING, pid=1)
    store.update(job.id, status=FAILED, error="boom")
    store.retry(job.id, max_retries=99)
    assert len(store.get(job.id).attempt_history) <= 50


# --------------------------------------------------------------------- runner


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """A pipeline that does not touch ffmpeg, so runner tests stay fast."""
    calls: list[str] = []
    delay = {"seconds": 0.0}
    state = {"fail_next": False}

    class FakePlan:
        source = ""
        providers = {"asr": "stub", "tts": "stub", "translate": "stub", "separator": "none"}
        problems: list = []
        estimates = {"usd": 0.0, "minutes": 0.1}

    class FakeResult:
        output = ""
        audio = ""
        transcript_json = ""
        subtitles = {}
        report = ""
        review = ""  # the pipeline writes review.json since 0.4.0
        providers_used = {"asr": "stub", "tts": "stub"}
        warnings: list = []
        retries: list = []

        def __init__(self, ok=True):
            self.qc = {
                "ok": ok,
                "failures": [] if ok else ["translated"],
                "warnings": [],
                "metrics": {"rendered": 1, "segments": 1},
            }
            self.ok = ok

    def fake_plan(source, settings=None):
        calls.append("plan")
        plan = FakePlan()
        plan.source = str(source)
        return plan

    def fake_dub(source, settings=None, output=None, log=None, cancel=None, progress=None):
        calls.append("dub")
        if progress:
            progress("synthesise", 1, 11)
        time.sleep(delay["seconds"])
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("provider exploded on the first attempt")
        result = FakeResult()
        result.output = str(output or "")
        return result

    monkeypatch.setattr(pipeline_module, "plan", fake_plan)
    monkeypatch.setattr(pipeline_module, "dub", fake_dub)
    return {"calls": calls, "delay": delay, "state": state}


def test_the_runner_completes_a_job_and_releases_its_claim(tmp_path, fake_pipeline):
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=1)
    runner = JobRunner(settings)
    try:
        job = runner.submit(source, settings, tmp_path / "out.mp4")
        final = runner.wait(job.id, timeout=60)
        assert final.status == COMPLETED
        assert final.attempts == 1
        assert final.claim == {}
        assert not (tmp_path / "work" / "registry" / f"{job.id}.claim").exists()
        assert final.attempt_history
    finally:
        runner.shutdown(wait=True)


def test_a_second_runner_does_not_run_the_same_request_twice(tmp_path, fake_pipeline):
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=2)
    fake_pipeline["delay"]["seconds"] = 0.5
    first = JobRunner(settings)
    second = JobRunner(settings)
    try:
        job = first.submit(source, settings, tmp_path / "out.mp4")
        time.sleep(0.05)
        duplicate = second.submit(source, settings, tmp_path / "out.mp4")
        assert duplicate.id == job.id
        assert duplicate.duplicate is True
        final = first.wait(job.id, timeout=60)
        assert final.status == COMPLETED
        assert fake_pipeline["calls"].count("dub") == 1, "the same request ran twice"
    finally:
        first.shutdown(wait=True)
        second.shutdown(wait=True)


def test_shutdown_interrupts_running_jobs_and_stops_the_heartbeat(tmp_path, fake_pipeline):
    source = _source(tmp_path)
    settings = Settings(
        target_lang="hi",
        workdir=str(tmp_path / "work"),
        job_heartbeat_interval_s=1.0,
        max_concurrent_jobs=1,
    )
    fake_pipeline["delay"]["seconds"] = 30.0
    runner = JobRunner(settings)
    job = runner.submit(source, settings, tmp_path / "out.mp4")
    time.sleep(0.5)
    assert runner._heartbeats, "a heartbeat thread should be running"
    runner.shutdown(wait=False)

    final = JobStore(tmp_path / "work").get(job.id, recover=False)
    assert final.status in (INTERRUPTED, FAILED, CANCELLED)
    assert final.status != COMPLETED
    deadline = time.time() + 10
    while time.time() < deadline and any(
        thread.name.startswith("dubforge-hb") and thread.is_alive()
        for thread in threading.enumerate()
    ):
        time.sleep(0.1)
    assert not [
        t for t in threading.enumerate() if t.name.startswith("dubforge-hb") and t.is_alive()
    ]


def test_a_retried_job_runs_with_its_original_settings(tmp_path, fake_pipeline):
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=1)
    fake_pipeline["state"]["fail_next"] = True
    runner = JobRunner(settings)
    try:
        job = runner.submit(source, settings, tmp_path / "out.mp4")
        failed = runner.wait(job.id, timeout=60)
        assert failed.status == FAILED
        retried = runner.retry(job.id)
        final = runner.wait(retried.id, timeout=60)
        assert final.status == COMPLETED
        assert final.attempts == 2
        assert any(entry.get("attempt") == 2 for entry in final.attempt_history)
    finally:
        runner.shutdown(wait=True)


# ------------------------------------------------------------------- events


def test_events_are_off_unless_asked_for(tmp_path, monkeypatch, fake_pipeline):
    monkeypatch.delenv("DUBFORGE_EVENTS", raising=False)
    from dubforge import events

    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=1)
    runner = JobRunner(settings)
    try:
        job = runner.submit(source, settings, tmp_path / "out.mp4")
        runner.wait(job.id, timeout=60)
    finally:
        runner.shutdown(wait=True)
    assert events.read() == []


def test_events_describe_a_job_without_secrets(tmp_path, monkeypatch, fake_pipeline):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("DUBFORGE_EVENTS", str(log))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear")
    from dubforge import events

    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=1)
    runner = JobRunner(settings)
    try:
        job = runner.submit(source, settings, tmp_path / "out.mp4")
        runner.wait(job.id, timeout=60)
    finally:
        runner.shutdown(wait=True)

    records = events.read(str(log))
    names = [record["event"] for record in records]
    assert events.JOB_CREATED in names
    assert events.JOB_CLAIMED in names
    assert events.JOB_STAGE in names
    assert events.JOB_COMPLETED in names
    assert events.QC_RESULT in names
    blob = log.read_text(encoding="utf-8")
    assert "sk-must-not-appear" not in blob
    assert all("at" in record for record in records)


def test_an_unwritable_event_log_never_breaks_the_work(tmp_path, monkeypatch, fake_pipeline):
    blocker = tmp_path / "events-dir"
    blocker.mkdir()
    monkeypatch.setenv("DUBFORGE_EVENTS", str(blocker))  # a directory, not a file
    source = _source(tmp_path)
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"), max_concurrent_jobs=1)
    runner = JobRunner(settings)
    try:
        job = runner.submit(source, settings, tmp_path / "out.mp4")
        assert runner.wait(job.id, timeout=60).status == COMPLETED
    finally:
        runner.shutdown(wait=True)


def test_a_completed_job_always_carries_a_verdict(store, settings, tmp_path):
    """`completed` and `qc_ok: None` must never coexist: that is an undecided dub."""
    source = _source(tmp_path)
    job = store.create(source, settings)
    store.update(job.id, status=PROCESSING, pid=1)
    done = store.update(job.id, status=COMPLETED)  # no verdict supplied
    assert done.qc_ok is False
    assert "completed without a QC verdict" in done.warnings
