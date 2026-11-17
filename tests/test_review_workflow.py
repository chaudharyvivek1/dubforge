"""The review loop: export, validate, apply — and every way a file can be refused."""

import json
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, pipeline, review
from dubforge.config import Settings
from dubforge.jobs import JobStore


@pytest.fixture()
def job(tmp_path):
    """A finished job with a review.json, ready to export."""
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="ffmpeg_center",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
    )
    result = pipeline.dub(clip, settings, output=tmp_path / "first.mp4")
    store = JobStore(settings.workdir)
    record = store.create(clip, settings)
    store.update(record.id, status="processing", pid=1)
    store.update(
        record.id,
        status="completed",
        qc_ok=result.ok,
        transcript_json=result.transcript_json,
        output=result.output,
        audio=result.audio,
        subtitles=result.subtitles,
        report=result.report,
        review=result.review,
        qc={"ok": result.ok, "failures": result.qc["failures"]},
    )
    return settings, store.get(record.id), result


@needs_ffmpeg
def test_export_produces_one_editable_row_per_line(job, tmp_path):
    settings, record, _ = job
    payload = review.export(record.id, settings, out=tmp_path / "review.json")
    assert payload["schema"] == review.SCHEMA
    assert payload["job_id"] == record.id
    assert payload["source_fingerprint"] == record.fingerprint
    assert len(payload["segments"]) == 3
    row = payload["segments"][0]
    assert row["edited_target_text"] == row["target_text"]
    assert row["regenerate"] is False
    assert (tmp_path / "review.json").exists()


@needs_ffmpeg
def test_a_clean_export_validates(job, tmp_path):
    settings, record, _ = job
    review.export(record.id, settings, out=tmp_path / "r.json")
    verdict = review.validate(tmp_path / "r.json", settings)
    assert verdict["ok"] is True
    assert verdict["edits"] == 0


@needs_ffmpeg
def test_an_edit_is_applied_and_only_that_line_is_rendered(job, tmp_path):
    settings, record, _ = job
    review.export(record.id, settings, out=tmp_path / "r.json")
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    data["segments"][0]["edited_target_text"] = "यह एक संपादित पंक्ति है।"
    (tmp_path / "r.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    outcome = review.apply(tmp_path / "r.json", settings)
    assert outcome["applied"] == 1
    assert outcome["rendered_segments"] == 1
    assert outcome["reused_segments"] == 2
    assert outcome["stems_reused"] == 1, "the bed must come from the stem cache"
    assert Path(outcome["output"]).exists()


@needs_ffmpeg
def test_applying_twice_is_a_no_op(job, tmp_path):
    settings, record, _ = job
    review.export(record.id, settings, out=tmp_path / "r.json")
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    data["segments"][0]["edited_target_text"] = "पहला संपादन।"
    (tmp_path / "r.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    first = review.apply(tmp_path / "r.json", settings)
    second = review.apply(tmp_path / "r.json", settings)
    assert first["applied"] == 1
    assert second.get("output") is None
    assert "no-op" in second["note"]


# ------------------------------------------------------------------- refusals


def _valid(tmp_path, job, mutate) -> Path:
    settings, record, _ = job
    review.export(record.id, settings, out=tmp_path / "r.json")
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    mutate(data)
    (tmp_path / "r.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp_path / "r.json"


@needs_ffmpeg
def test_an_unknown_schema_is_refused(job, tmp_path):
    path = _valid(tmp_path, job, lambda data: data.update({"schema": 99}))
    verdict = review.validate(path, job[0])
    assert verdict["ok"] is False and "schema" in verdict["problems"][0]


@needs_ffmpeg
def test_a_stale_review_from_another_source_is_refused(job, tmp_path):
    path = _valid(tmp_path, job, lambda data: data.update({"source_fingerprint": "other:123"}))
    verdict = review.validate(path, job[0])
    assert verdict["ok"] is False
    assert any("different version of the source" in problem for problem in verdict["problems"])


@needs_ffmpeg
def test_an_unknown_segment_is_refused(job, tmp_path):
    def mutate(data):
        data["segments"].append({"seg_id": "99999-deadbeef", "edited_target_text": "x"})

    verdict = review.validate(_valid(tmp_path, job, mutate), job[0])
    assert verdict["ok"] is False and any("unknown segment" in p for p in verdict["problems"])


@needs_ffmpeg
def test_a_duplicate_edit_is_refused(job, tmp_path):
    def mutate(data):
        row = dict(data["segments"][0])
        row["edited_target_text"] = "दूसरा संस्करण"
        data["segments"].append(row)

    verdict = review.validate(_valid(tmp_path, job, mutate), job[0])
    assert verdict["ok"] is False and any("more than once" in p for p in verdict["problems"])


@needs_ffmpeg
def test_oversized_text_is_refused(job, tmp_path):
    def mutate(data):
        data["segments"][0]["edited_target_text"] = "x" * (review.MAX_TEXT + 1)

    verdict = review.validate(_valid(tmp_path, job, mutate), job[0])
    assert verdict["ok"] is False and any("limit" in p for p in verdict["problems"])


@needs_ffmpeg
def test_a_non_string_translation_is_refused(job, tmp_path):
    def mutate(data):
        data["segments"][0]["edited_target_text"] = 42

    verdict = review.validate(_valid(tmp_path, job, mutate), job[0])
    assert verdict["ok"] is False and any("must be a string" in p for p in verdict["problems"])


@needs_ffmpeg
def test_a_path_or_token_cannot_be_set_through_a_review_file(job, tmp_path):
    def mutate(data):
        data["segments"][0]["token"] = "../../etc/passwd"
        data["segments"][0]["output"] = "/tmp/evil.mp4"

    verdict = review.validate(_valid(tmp_path, job, mutate), job[0])
    assert verdict["ok"] is False
    assert any("not editable" in problem for problem in verdict["problems"])


@needs_ffmpeg
def test_a_review_for_a_missing_job_is_refused(job, tmp_path):
    path = _valid(tmp_path, job, lambda data: data.update({"job_id": "nope"}))
    verdict = review.validate(path, job[0])
    assert verdict["ok"] is False and any("does not exist" in p for p in verdict["problems"])


def test_a_missing_file_is_a_problem_not_a_crash(tmp_path):
    verdict = review.validate(tmp_path / "absent.json", Settings(workdir=str(tmp_path / "w")))
    assert verdict["ok"] is False


def test_invalid_json_is_a_problem_not_a_crash(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    verdict = review.validate(path, Settings(workdir=str(tmp_path / "w")))
    assert verdict["ok"] is False and "not valid JSON" in verdict["problems"][0]


@needs_ffmpeg
def test_apply_refuses_a_file_that_does_not_validate(job, tmp_path):
    path = _valid(tmp_path, job, lambda data: data.update({"schema": 7}))
    with pytest.raises(review.ReviewError, match="schema"):
        review.apply(path, job[0])


# ---------------------------------------------------------------- the HTML page


@needs_ffmpeg
def test_the_html_review_page_is_static_and_local(job, tmp_path):
    settings, record, result = job
    page = Path(result.review).with_suffix(".html")
    assert page.exists()
    html = page.read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html
    assert "sentence-level" in html, "alignment precision must be visible to a reviewer"


@needs_ffmpeg
def test_the_html_marks_changed_lines(job, tmp_path):
    settings, record, _ = job
    payload = review.export(record.id, settings)
    payload["segments"][0]["edited_target_text"] = "बदला हुआ"
    html = review.to_html(payload)
    assert "class='changed'" in html
