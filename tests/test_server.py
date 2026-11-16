"""MCP surface: stable tool names, structured errors, no stack traces, no secrets."""

import asyncio
import json

import pytest

fastmcp = pytest.importorskip("fastmcp", reason="the MCP extra is not installed")

from dubforge.server import build_server  # noqa: E402

EXPECTED_TOOLS = {
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
