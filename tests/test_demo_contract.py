"""The CI gate must tell the intended QC failure apart from real breakage."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_demo_check  # noqa: E402
from conftest import needs_ffmpeg  # noqa: E402


def test_demo_exit_one_is_the_expected_outcome(tmp_path, monkeypatch):
    """Exit 1 + only the `translated` failure = the pipeline works. That is a pass."""
    monkeypatch.setattr(
        ci_demo_check,
        "run_demo",
        lambda out_dir, timeout=900: (1, json.dumps(_payload(tmp_path)), ""),
    )
    summary = ci_demo_check.check(tmp_path)
    assert summary["exit_code"] == 1
    assert summary["qc_failures"] == ["translated"]


def test_exit_zero_is_suspicious(tmp_path, monkeypatch):
    """If the stub pipeline suddenly passes QC, the `translated` check has been weakened."""
    monkeypatch.setattr(
        ci_demo_check,
        "run_demo",
        lambda out_dir, timeout=900: (0, json.dumps(_payload(tmp_path)), ""),
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "wrong_exit_code"


def test_a_crash_is_not_the_expected_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (2, "", "boom: ffmpeg exploded")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "demo_error"


def test_invalid_json_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (1, "{not json", "")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "invalid_json"


def test_missing_output_file_is_reported(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    payload["output"] = str(tmp_path / "gone.mp4")
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (1, json.dumps(payload), "")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "missing_output"


@needs_ffmpeg
def test_an_extra_qc_failure_is_not_ignored(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    payload["qc"]["failures"] = ["translated", "timing_fits"]
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (1, json.dumps(payload), "")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "unexpected_qc"


@needs_ffmpeg
def test_wrong_metrics_are_not_ignored(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    payload["qc"]["metrics"]["rendered"] = 0
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (1, json.dumps(payload), "")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "wrong_metrics"


@needs_ffmpeg
def test_an_audio_only_output_is_rejected(tmp_path, monkeypatch):
    """A dub with no picture is not a dub of a video."""
    from dubforge import ffmpeg

    audio_only = tmp_path / "audio-only.m4a"
    ffmpeg.write_tone_sequence(str(tmp_path / "t.wav"), [(0.0, 2.0, 440.0)], 2.0)
    ffmpeg.mux_audio_only(tmp_path / "t.wav", audio_only)
    payload = _payload(tmp_path)
    payload["output"] = str(audio_only)
    monkeypatch.setattr(
        ci_demo_check, "run_demo", lambda out_dir, timeout=900: (1, json.dumps(payload), "")
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "missing_streams"


# --------------------------------------------------------------------- fixtures


def _payload(tmp_path: Path) -> dict:
    """A payload shaped like `dubforge demo --json`, with a real playable output."""
    from dubforge import demo

    clip = demo.build_clip(tmp_path / "demo-dubbed.hi.mp4")
    return {
        "output": clip,
        "qc_ok": False,
        "qc": {
            "ok": False,
            "failures": ["translated"],
            "warnings": [],
            "metrics": {
                "segments": 3,
                "rendered": 3,
                "worst_overflow_s": 0.0,
                "speech_seconds_dubbed": 8.0,
                "input_seconds": 9.2,
                "output_seconds": 9.2,
            },
            "checks": [
                {"check": "every_line_has_audio", "ok": True, "severity": "pass", "detail": ""},
                {"check": "timing_fits", "ok": True, "severity": "pass", "detail": ""},
                {"check": "speech_rate", "ok": True, "severity": "pass", "detail": ""},
                {"check": "dub_is_audible", "ok": True, "severity": "pass", "detail": ""},
            ],
        },
    }


def test_a_crash_that_exits_one_is_not_the_expected_failure(tmp_path, monkeypatch):
    """An uncaught exception also exits 1 — the same code as the intended QC failure."""
    monkeypatch.setattr(
        ci_demo_check,
        "run_demo",
        lambda out_dir, timeout=900: (1, "", "Traceback (most recent call last):\n  boom"),
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "demo_error"
    assert "traceback" in exc.value.detail.lower()


def test_empty_stdout_names_the_encoding_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ci_demo_check,
        "run_demo",
        lambda out_dir, timeout=900: (1, "", "UnicodeEncodeError: 'charmap' codec can't encode"),
    )
    with pytest.raises(ci_demo_check.DemoCheckFailed) as exc:
        ci_demo_check.check(tmp_path)
    assert exc.value.code == "invalid_json"
    assert "stderr tail" in exc.value.detail
    assert "UnicodeEncodeError" in exc.value.detail


@needs_ffmpeg
def test_json_output_survives_a_legacy_windows_code_page(tmp_path):
    """The real Windows bug: a `→` in the payload killed the command with exit 1."""
    import os
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "dubforge", "demo", "--out-dir", str(tmp_path / "demo"), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        timeout=900,
    )
    assert proc.returncode == 1, proc.stderr[-500:]
    assert "UnicodeEncodeError" not in proc.stderr
    payload = json.loads(proc.stdout)  # must be valid JSON, not mojibake
    assert payload["qc_ok"] is False
    assert payload["qc"]["failures"] == ["translated"]
    # and the machine output is pure ASCII, so no console can break it
    assert proc.stdout.isascii()
