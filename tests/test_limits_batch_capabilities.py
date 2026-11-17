"""Limits, capabilities and batch behaviour: the guard rails an agent runs into."""

from pathlib import Path

import pytest

from dubforge import batch, capabilities, limits, pipeline
from dubforge.config import Settings
from dubforge.jobs import JobStore
from dubforge.providers import registry

# ---------------------------------------------------------------------- limits


def _file(tmp_path, name="clip.mp4", size=1024):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


def test_a_file_over_the_size_limit_is_refused_with_the_numbers(tmp_path):
    path = _file(tmp_path, size=4096)
    settings = Settings(max_input_bytes=1024)
    with pytest.raises(limits.LimitExceeded) as exc:
        limits.check_source(path, settings, duration=1.0)
    assert exc.value.kind == "max_input_bytes"
    assert "DUBFORGE_MAX_INPUT_BYTES" in exc.value.hint
    assert exc.value.facts["bytes"] == 4096


def test_a_file_over_the_duration_limit_is_refused(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(limits.LimitExceeded) as exc:
        limits.check_source(path, Settings(max_input_seconds=60), duration=3600)
    assert exc.value.kind == "max_input_seconds"
    assert "min" in str(exc.value)


def test_a_file_within_the_limits_passes_and_reports_facts(tmp_path):
    facts = limits.check_source(_file(tmp_path), Settings(), duration=12.5)
    assert facts.seconds == 12.5
    assert facts.bytes == 1024


def test_an_estimate_over_budget_is_refused():
    with pytest.raises(limits.LimitExceeded) as exc:
        limits.check_estimate({"usd": 99.0}, Settings(max_estimated_usd=5.0))
    assert exc.value.kind == "max_estimated_usd"
    assert "not a quote" in exc.value.hint


def test_an_unknown_estimate_is_not_a_failure():
    limits.check_estimate({"usd": None}, Settings(max_estimated_usd=0.01))
    limits.check_estimate({}, Settings(max_estimated_usd=0.01))


def test_the_runtime_limit_fires_with_elapsed_time():
    import time

    with pytest.raises(limits.LimitExceeded) as exc:
        limits.check_runtime(time.time() - 120, Settings(max_run_seconds=60))
    assert exc.value.kind == "max_run_seconds"


def test_limits_are_overridable_from_the_environment(monkeypatch):
    monkeypatch.setenv("DUBFORGE_MAX_INPUT_SECONDS", "30")
    monkeypatch.setenv("DUBFORGE_MAX_CONCURRENT_JOBS", "1")
    settings = Settings.from_env()
    assert settings.max_input_seconds == 30
    assert settings.max_concurrent_jobs == 1
    assert limits.describe(settings)["max_input_seconds"] == 30


def test_limits_are_validated(monkeypatch):
    assert Settings(max_input_seconds=0).validate()
    assert Settings(max_concurrent_jobs=0).validate()
    assert Settings(max_estimated_usd=-1).validate()


def test_plan_reports_a_limit_breach_as_a_problem(demo_clip, tmp_path, monkeypatch):
    """A plan that cannot be afforded is not ready, and says why."""
    monkeypatch.setattr(pipeline, "limits_mod", limits)
    from dubforge import pricing

    monkeypatch.setattr(pricing, "estimate", lambda *a, **k: 500.0)
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        workdir=str(tmp_path / "w"),
        max_estimated_usd=1.0,
    )
    plan = pipeline.plan(demo_clip, settings)
    assert not plan.ready
    assert any("max_estimated_usd" in problem.lower() for problem in plan.problems), plan.problems


# ---------------------------------------------------------------- capabilities


def test_capabilities_are_derived_from_the_registry(tmp_path):
    snapshot = capabilities.snapshot(Settings(workdir=str(tmp_path / "w")))
    for kind in ("asr", "translate", "tts", "separator", "lipsync"):
        assert kind in snapshot["providers"]
        names = {row["name"] for row in snapshot["providers"][kind]}
        assert names == set(registry.names(kind))


def test_capabilities_report_readiness_and_verification_honestly(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    snapshot = capabilities.snapshot(Settings(workdir=str(tmp_path / "w")))
    openai_rows = [row for row in snapshot["providers"]["tts"] if row["name"] == "openai"]
    assert openai_rows[0]["ready"] is False
    assert openai_rows[0]["verified"] is False
    assert openai_rows[0]["missing"]


def test_a_stub_does_not_make_a_feature_supported(tmp_path):
    """A test double must never be reported as a capability an agent can rely on."""
    snapshot = capabilities.snapshot(Settings(workdir=str(tmp_path / "w")))
    assert snapshot["features"]["voice_cloning"]["supported"] is False
    assert snapshot["features"]["diarization"]["provider"] != "stub"


def test_capabilities_include_limits_and_formats(tmp_path):
    snapshot = capabilities.snapshot(Settings(workdir=str(tmp_path / "w")))
    assert snapshot["limits"]["max_concurrent_jobs"] >= 1
    assert "mp4" in snapshot["formats"]["output_containers"]
    assert snapshot["formats"]["video_stream"].startswith("copied")


def test_doctor_and_capabilities_agree_on_readiness(tmp_path):
    from dubforge import doctor

    settings = Settings(workdir=str(tmp_path / "w"))
    report = doctor.checks(settings)
    snapshot = capabilities.snapshot(settings)
    for kind, ready in report["ready"].items():
        assert ready == (snapshot["auto"][kind] is not None)


# --------------------------------------------------------------------- batch


@pytest.fixture()
def lesson_folder(tmp_path):
    from dubforge import demo

    folder = tmp_path / "lessons"
    folder.mkdir()
    demo.build_clip(folder / "a.mp4")
    demo.build_clip(folder / "b.mp4")
    return folder


def _batch_settings(tmp_path, script):
    return Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=script,
        workdir=str(tmp_path / "work"),
        cache=False,
        max_concurrent_jobs=2,
    )


def test_collect_sources_finds_media_and_ignores_other_files(lesson_folder):
    (lesson_folder / "notes.txt").write_text("x", encoding="utf-8")
    found = batch.collect_sources(lesson_folder)
    assert [p.name for p in found] == ["a.mp4", "b.mp4"]


def test_collect_sources_never_feeds_on_its_own_output(lesson_folder, tmp_path):
    from dubforge import demo

    settings = _batch_settings(tmp_path, demo.DEMO_SCRIPT)
    store = JobStore(settings.workdir)
    job = store.create(lesson_folder / "a.mp4", settings)
    store.update(job.id, status="completed", output=str(lesson_folder / "a.hi.mp4"), qc_ok=True)
    (lesson_folder / "a.hi.mp4").write_bytes(b"dub output")

    found = batch.collect_sources(lesson_folder, store=store)
    assert "a.hi.mp4" not in [p.name for p in found]


def test_batch_writes_outside_the_input_folder(lesson_folder, tmp_path):
    from dubforge import demo

    settings = _batch_settings(tmp_path, demo.DEMO_SCRIPT)
    sources = batch.collect_sources(lesson_folder, store=JobStore(settings.workdir))
    result = batch.run_batch(sources, settings)
    for item in result.items:
        assert item.output
        assert lesson_folder not in __import__("pathlib").Path(item.output).parents
        assert __import__("pathlib").Path(item.output).exists()


def test_batch_isolates_failures(lesson_folder, tmp_path, monkeypatch):
    """One broken file must not stop the rest of a class set."""
    from dubforge import demo

    settings = _batch_settings(tmp_path, demo.DEMO_SCRIPT)
    sources = batch.collect_sources(lesson_folder, store=JobStore(settings.workdir))
    (lesson_folder / "broken.mp4").write_bytes(b"not a video at all")
    sources.append(lesson_folder / "broken.mp4")

    result = batch.run_batch(sources, settings)
    statuses = {Path(item.source).name: item.status for item in result.items}
    assert statuses["broken.mp4"] == "failed"
    assert statuses["a.mp4"] == "completed"
    assert statuses["b.mp4"] == "completed"
    assert result.ok is False  # one file failed, so the batch is not clean


def test_batch_counts_and_resume(lesson_folder, tmp_path):
    from dubforge import demo

    settings = _batch_settings(tmp_path, demo.DEMO_SCRIPT)
    sources = batch.collect_sources(lesson_folder, store=JobStore(settings.workdir))
    first = batch.run_batch(sources, settings)
    assert first.counts["total"] == 2
    assert first.counts["failed"] == 0
    # stub QC fails, so nothing is skipped on the second pass — correct behaviour
    second = batch.run_batch(sources, settings)
    assert second.counts["skipped"] == 0


def test_output_for_tags_the_language_once(tmp_path):
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "w"))
    first = batch.output_for(tmp_path / "lesson.mp4", settings)
    assert first.name == "lesson.hi.mp4"
    again = batch.output_for(tmp_path / "lesson.hi.mp4", settings)
    assert again.name == "lesson.hi.mp4"
    audio = batch.output_for(tmp_path / "talk.mp3", settings)
    assert audio.suffix == ".m4a"
