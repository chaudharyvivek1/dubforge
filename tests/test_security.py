"""Security invariants that are cheap to check and expensive to get wrong."""

import json
import re
from pathlib import Path

import pytest

from dubforge import doctor, ffmpeg
from dubforge.config import Settings
from dubforge.providers import registry

SRC = Path(__file__).resolve().parents[1] / "src" / "dubforge"


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_shell_true_anywhere():
    """Every subprocess call takes a list. A shell would make a filename an injection."""
    offenders = [
        path.name
        for path in _sources()
        if re.search(r"shell\s*=\s*True", path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"shell=True found in {offenders}"


def test_no_os_system_or_popen_with_a_string():
    offenders = [
        path.name
        for path in _sources()
        if re.search(r"os\.(system|popen)\s*\(", path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"os.system/os.popen found in {offenders}"


def test_ffmpeg_arguments_are_always_a_list():
    """A `run("…")` string argument would go straight into subprocess."""
    source = (SRC / "ffmpeg.py").read_text(encoding="utf-8")
    assert "cmd = [ffmpeg_path()" in source
    for match in re.finditer(r"subprocess\.run\(\s*\[?", source):
        snippet = source[match.start() : match.start() + 120]
        assert not snippet.startswith("subprocess.run(\n        f"), snippet


def test_a_key_is_never_written_to_a_report_or_a_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-report-must-not-contain-this")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-report-must-not-contain-this")
    report = doctor.checks(Settings(workdir=str(tmp_path / "w")))
    blob = json.dumps(report)
    assert "sk-report-must-not-contain-this" not in blob
    assert "el-report-must-not-contain-this" not in blob
    assert report["env"]["OPENAI_API_KEY"] is True


def test_provider_errors_name_the_variable_not_its_value(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    provider = registry.create("asr", "deepgram", Settings())
    message = "; ".join(provider.missing())
    assert "DEEPGRAM_API_KEY" in message
    assert "=" not in message.replace("is not set", "")


def test_http_helpers_never_put_a_header_in_an_error():
    source = (SRC / "providers" / "http.py").read_text(encoding="utf-8")
    # error strings are built from status codes and response bodies only
    assert "resp.status_code" in source
    for match in re.finditer(r"raise ProviderError\(([^)]*)\)", source):
        assert "headers" not in match.group(1)


def test_cleanup_cannot_delete_outside_the_job(tmp_path):
    from dubforge.workspace import open_job

    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"x")
    job = open_job(tmp_path / "work", source, fingerprint="f")
    try:
        outside = tmp_path / "keep"
        outside.mkdir()
        (outside / "precious.txt").write_text("x", encoding="utf-8")
        removed = job.cleanup()
        assert isinstance(removed, list)
        assert (outside / "precious.txt").exists()
    finally:
        job.release()


def test_output_cannot_overwrite_the_source(tmp_path, monkeypatch):
    """Caught before the first provider call, not by ffmpeg at the last step."""
    from dubforge import pipeline

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not really a video")
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "w"))

    # probe would fail on this junk file; the guard must fire first
    with pytest.raises(pipeline.DubError) as exc:
        pipeline.dub(source, settings, output=source)
    assert "overwrite the source" in str(exc.value)


def test_workdir_is_created_inside_the_requested_root(tmp_path):
    from dubforge.workspace import open_job

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    job = open_job(tmp_path / "work", source, fingerprint="f")
    try:
        assert str(job.path).startswith(str(tmp_path / "work"))
        assert str(job.cache_dir).startswith(str(tmp_path / "work"))
    finally:
        job.release()


def test_transcript_text_is_never_executed():
    """Transcripts are data. Nothing in the package eval()s or exec()s them."""
    offenders = [
        path.name
        for path in _sources()
        if re.search(r"\b(eval|exec)\s*\(", path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"eval/exec found in {offenders}"


def test_ffmpeg_error_messages_do_not_include_the_command_environment():
    try:
        ffmpeg.run(["-i", "/nonexistent-input", "/tmp/out.wav"])
    except ffmpeg.FFmpegError as exc:
        message = str(exc)
        assert "OPENAI_API_KEY" not in message
        assert len(message) < 4000
    else:
        pytest.skip("ffmpeg accepted a missing input")
