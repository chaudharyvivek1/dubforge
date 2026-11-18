"""MCP surface: stable tool names, structured errors, no stack traces, no secrets."""

import asyncio
import json

import pytest

fastmcp = pytest.importorskip("fastmcp", reason="the MCP extra is not installed")

from dubforge.config import Settings  # noqa: E402
from dubforge.server import build_server  # noqa: E402

EXPECTED_TOOLS = {
    # the original ten: names are a public contract and never change
    "dubforge_doctor",
    "list_providers",
    "probe_media",
    "plan_dub",
    "transcribe_video",
    "dub_video",
    "get_qc",
    "list_voices",
    "write_subtitles",
    "dubforge_version",
    # job control
    "dubforge_submit_job",
    "dubforge_get_job_status",
    "dubforge_get_job_result",
    "dubforge_list_jobs",
    "dubforge_cancel_job",
    "dubforge_retry_job",
    "dubforge_cleanup_jobs",
    # regeneration
    "dubforge_regenerate_segments",
    # review workflow
    "dubforge_explain_job",
    "dubforge_verification_status",
    "dubforge_export_review",
    "dubforge_validate_review",
    "dubforge_apply_review",
    # intelligence
    "dubforge_get_capabilities",
    "dubforge_validate_config",
    "dubforge_check_provider_health",
    "dubforge_estimate_cost",
}


def call(name: str, arguments: dict) -> dict:
    """Call one tool in-process and return the parsed JSON payload."""

    async def run() -> dict:
        async with fastmcp.Client(build_server()) as client:
            result = await client.call_tool(name, arguments)
            text = result.content[0].text
            assert "Traceback" not in text, "a tool leaked a stack trace"
            return json.loads(text)

    return asyncio.run(run())


def test_tool_names_are_stable():
    async def run() -> set[str]:
        async with fastmcp.Client(build_server()) as client:
            return {tool.name for tool in await client.list_tools()}

    assert asyncio.run(run()) == EXPECTED_TOOLS


def test_every_error_has_a_code_message_and_hint():
    payload = call("plan_dub", {"path": "/nope/missing.mp4", "target_lang": "hi"})
    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "missing_file"
    assert "does not exist" in error["message"]
    assert error["hint"]


def test_missing_file_codes():
    assert call("probe_media", {"path": "/nope.mp4"})["error"]["code"] == "missing_file"
    assert call("get_qc", {"workdir": "/nope"})["error"]["code"] == "missing_file"
    assert (
        call("write_subtitles", {"transcript_json": "/nope.json"})["error"]["code"]
        == "missing_file"
    )


def test_unreadable_media_is_unsupported_not_a_crash(tmp_path):
    junk = tmp_path / "not-media.mp4"
    junk.write_bytes(b"definitely not a video")
    assert call("probe_media", {"path": str(junk)})["error"]["code"] == "unsupported_media"


def test_invalid_language_codes_are_rejected_with_an_example(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    payload = call("plan_dub", {"path": str(clip), "target_lang": "Hindi (India)"})
    assert payload["error"]["code"] == "invalid_argument"
    assert "hi" in payload["error"]["hint"]
    assert (
        call("list_voices", {"provider": "stub", "language": "tamil"})["error"]["code"]
        == "invalid_argument"
    )


def test_out_of_range_numbers_are_rejected_with_a_code(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    for tool, arguments in (
        ("plan_dub", {"path": str(clip), "target_lang": "hi", "max_speed": -5}),
        ("plan_dub", {"path": str(clip), "target_lang": "hi", "max_speed": 99}),
        ("dub_video", {"path": str(clip), "target_lang": "hi", "rewrite_rounds": 99}),
        ("dub_video", {"path": str(clip), "target_lang": "hi", "loudness_lufs": 0}),
    ):
        payload = call(tool, arguments)
        assert payload["error"]["code"] == "invalid_argument", arguments


def test_wrong_types_are_rejected_by_the_tool_schema(tmp_path):
    """A string where a number belongs never reaches the body: the MCP schema refuses it."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with pytest.raises(Exception) as exc:  # fastmcp raises its own ToolError
        call("plan_dub", {"path": str(clip), "target_lang": "hi", "max_speed": "fast"})
    assert "max_speed" in str(exc.value)


def test_unknown_providers_list_the_alternatives(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    payload = call("dub_video", {"path": str(clip), "target_lang": "hi", "asr": "nope"})
    assert payload["error"]["code"] == "invalid_argument"
    assert "deepgram" in payload["error"]["hint"]
    assert call("list_voices", {"provider": "nope"})["error"]["code"] == "invalid_argument"
    assert call("list_providers", {"kind": "video"})["error"]["code"] == "invalid_argument"


def test_malformed_glossary_and_voice_map(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    for field, value in (("glossary", "ISRO"), ("voice_map", "SPEAKER_00")):
        payload = call("dub_video", {"path": str(clip), "target_lang": "hi", field: value})
        assert payload["error"]["code"] == "invalid_argument", field
        assert "=" in payload["error"]["hint"]


def test_output_cannot_be_the_source(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    payload = call("dub_video", {"path": str(clip), "target_lang": "hi", "output": str(clip)})
    assert payload["error"]["code"] == "invalid_argument"
    assert "overwrite" in payload["error"]["message"]


def test_malformed_transcript_file(tmp_path):
    path = tmp_path / "t.json"
    path.write_text('{"not": "a transcript"}', encoding="utf-8")
    payload = call("write_subtitles", {"transcript_json": str(path)})
    assert payload["error"]["code"] == "invalid_argument"
    assert "segments" in payload["error"]["message"]


def test_missing_provider_dependencies_are_reported_as_no_provider(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    payload = call("transcribe_video", {"path": str(clip), "asr": "deepgram"})
    if payload["ok"]:  # a key happens to be configured in this environment
        pytest.skip("a Deepgram key is configured here")
    assert payload["error"]["code"] in {"no_provider", "unsupported_media"}


def test_doctor_and_version_payloads():
    version = call("dubforge_version", {})
    assert version["ok"] is True
    assert "internal_error" in version["error_codes"]

    doctor = call("dubforge_doctor", {})
    assert doctor["ok"] is True
    assert "stages" in doctor and "packages" in doctor


def test_no_secret_is_echoed_by_any_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-must-never-appear")
    payload = call("dubforge_doctor", {})
    assert "sk-live-must-never-appear" not in json.dumps(payload)
    assert payload["env"]["OPENAI_API_KEY"] is True


# ------------------------------------------------------------------ job tools


def _demo_clip(tmp_path):
    from dubforge import demo

    return demo.build_clip(tmp_path / "clip.mp4")


def test_job_tools_round_trip(tmp_path, monkeypatch):
    """submit → status → result, through the real tool surface."""
    clip = _demo_clip(tmp_path)
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("DUBFORGE_SCRIPT", "SPEAKER_00: hello\nSPEAKER_01: world\nSPEAKER_00: again")

    submitted = call(
        "dubforge_submit_job",
        {
            "path": str(clip),
            "target_lang": "hi",
            "asr": "stub",
            "translator": "stub",
            "tts": "stub",
            "separator": "none",
        },
    )
    assert submitted["ok"] is True, submitted
    job_id = submitted["job"]["job_id"]

    status = call("dubforge_get_job_status", {"job_id": job_id})
    assert status["ok"] is True
    assert status["job"]["status"] in {
        "queued",
        "validating",
        "processing",
        "quality_check",
        "completed",
        "failed",
    }

    from dubforge.jobs import JobRunner

    # wait through the runner the server itself uses
    runner = JobRunner(Settings(workdir=str(tmp_path / "work")))
    final = runner.wait(job_id, timeout=180)
    runner.shutdown()
    assert final.status == "completed"
    assert final.qc_ok is False  # the stub translator is flagged, by design

    result = call("dubforge_get_job_result", {"job_id": job_id})
    assert result["ok"] is True
    assert result["qc"]["failures"] == ["translated"]
    assert result["providers_used"]["asr"] == "stub"


def test_get_job_result_refuses_an_unfinished_job(tmp_path, monkeypatch):
    from dubforge.jobs import JobStore

    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    store = JobStore(tmp_path / "work")
    source = tmp_path / "x.mp4"
    source.write_bytes(b"x")
    job = store.create(source, Settings(target_lang="hi", workdir=str(tmp_path / "work")))
    payload = call("dubforge_get_job_result", {"job_id": job.id})
    assert payload["error"]["code"] == "invalid_argument"
    assert "still queued" in payload["error"]["message"]


def test_list_jobs_validates_status_and_paginates(tmp_path, monkeypatch):
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    bad = call("dubforge_list_jobs", {"status": "nonsense"})
    assert bad["error"]["code"] == "invalid_argument"
    ok = call("dubforge_list_jobs", {"limit": 5, "offset": 0})
    assert ok["ok"] is True
    assert ok["total"] >= 0
    assert ok["returned"] <= 5


def test_cleanup_defaults_to_a_dry_run(tmp_path, monkeypatch):
    from dubforge.jobs import JobStore

    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    store = JobStore(tmp_path / "work")
    source = tmp_path / "x.mp4"
    source.write_bytes(b"x")
    job = store.create(source, Settings(target_lang="hi", workdir=str(tmp_path / "work")))
    store.update(job.id, status="processing", pid=1)
    store.update(job.id, status="completed", qc_ok=True)
    payload = call("dubforge_cleanup_jobs", {})
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert job.id in payload["removed"]
    assert store.get(job.id) is not None, "a dry run must not delete anything"


def test_cancel_and_retry_are_reachable(tmp_path, monkeypatch):
    from dubforge.jobs import JobStore

    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    store = JobStore(tmp_path / "work")
    source = tmp_path / "x.mp4"
    source.write_bytes(b"x")
    job = store.create(source, Settings(target_lang="hi", workdir=str(tmp_path / "work")))
    cancelled = call("dubforge_cancel_job", {"job_id": job.id})
    assert cancelled["ok"] is True
    assert cancelled["job"]["status"] == "cancelled"
    retried = call("dubforge_retry_job", {"job_id": job.id})
    assert retried["ok"] is True, retried


# ------------------------------------------------------------- intelligence tools


def test_capabilities_tool_matches_the_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    from dubforge import capabilities

    payload = call("dubforge_get_capabilities", {})
    assert payload["ok"] is True
    assert payload["capabilities"]["stages"] == list(capabilities.STAGES)


def test_validate_config_tool_reports_problems(tmp_path, monkeypatch):
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    bad = tmp_path / "settings.json"
    bad.write_text('{"max_speed": "fast"}', encoding="utf-8")
    payload = call("dubforge_validate_config", {"config_path": str(bad)})
    assert payload["ok"] is True, payload  # the tool call itself succeeded
    assert payload["valid"] is False
    assert payload["problems"], "an invalid file must produce problems"
    assert any("max_speed" in problem for problem in payload["problems"])


def test_provider_health_tool_is_ready_only_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    payload = call("dubforge_check_provider_health", {"kind": "tts", "live": False})
    assert payload["ok"] is True
    rows = payload["providers"]["tts"]
    assert {row["provider"] for row in rows} >= {"elevenlabs", "stub"}
    assert all(row["live"] is None for row in rows), "live=false must not touch the network"
    bad = call("dubforge_check_provider_health", {"kind": "video"})
    assert bad["error"]["code"] == "invalid_argument"


def test_estimate_cost_tool_spends_nothing(tmp_path, monkeypatch):
    clip = _demo_clip(tmp_path)
    monkeypatch.setenv("DUBFORGE_WORKDIR", str(tmp_path / "work"))
    payload = call(
        "dubforge_estimate_cost",
        {
            "path": str(clip),
            "target_lang": "hi",
            "asr": "stub",
            "translator": "stub",
            "tts": "stub",
        },
    )
    assert payload["ok"] is True
    assert "estimates" in payload and "limits" in payload
    assert payload["allowed"] is True
