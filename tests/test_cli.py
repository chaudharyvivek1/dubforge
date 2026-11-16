"""The CLI must be usable without reading the source, and must exit non-zero on a bad dub."""

import json

import pytest
from conftest import needs_ffmpeg

from dubforge.cli import EXIT_ERROR, EXIT_OK, EXIT_QC_FAILED, main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "dubforge" in capsys.readouterr().out


def test_doctor_runs_and_is_honest(capsys, tmp_path):
    code = main(["doctor", "--workdir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "ffmpeg" in out
    assert "not set" in out or "set" in out
    assert "sk-" not in out  # no key material, ever


def test_doctor_json_parses(capsys, tmp_path):
    main(["doctor", "--json", "--workdir", str(tmp_path / "w")])
    data = json.loads(capsys.readouterr().out)
    assert "providers" in data
    assert data["python"]


def test_providers_lists_unverified(capsys):
    code = main(["providers"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "UNVERIFIED" in out
    assert "elevenlabs" in out


def test_providers_json(capsys):
    main(["providers", "--json", "--kind", "tts"])
    rows = json.loads(capsys.readouterr().out)
    assert rows and all(r["kind"] == "tts" for r in rows)


def test_missing_input_is_an_error_not_a_traceback(capsys, tmp_path):
    code = main(["plan", str(tmp_path / "nope.mp4"), "--to", "hi"])
    assert code == EXIT_ERROR
    assert "no such file" in capsys.readouterr().err


@needs_ffmpeg
def test_demo_cli_exits_nonzero_on_stub_qc(capsys, tmp_path):
    """Exit 1 is correct here: the demo is a plumbing test, and QC says so."""
    code = main(["demo", "--out-dir", str(tmp_path / "d"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["qc_ok"] is False
    assert code == EXIT_QC_FAILED


@needs_ffmpeg
def test_plan_cli_on_the_demo_clip(capsys, tmp_path):
    from dubforge import demo

    clip = demo.build_clip(tmp_path / "c.mp4")
    code = main(
        [
            "plan",
            clip,
            "--to",
            "hi",
            "--asr",
            "stub",
            "--translator",
            "stub",
            "--tts",
            "stub",
            "--workdir",
            str(tmp_path / "w"),
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "providers" in out


def test_srt_cli_from_transcript(capsys, tmp_path):
    from dubforge.media import Segment, Transcript

    path = Transcript(
        segments=[Segment(index=0, start=0.0, end=1.0, text="a", target_text="b")]
    ).save(tmp_path / "t.json")
    code = main(["srt", path, "--out-dir", str(tmp_path)])
    assert code == EXIT_OK
    assert (tmp_path / "dub.target.srt").exists() or (tmp_path / "dub.dub.srt").exists()
