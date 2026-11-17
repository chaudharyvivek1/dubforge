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


# ------------------------------------------------------------- 0.2.0 commands


def test_capabilities_command(capsys, tmp_path):
    code = main(["capabilities", "--json", "--workdir", str(tmp_path / "w")])
    data = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert data["stages"] == ["asr", "translate", "tts", "separator", "lipsync"]
    assert "limits" in data and "features" in data


def test_capabilities_command_in_prose(capsys, tmp_path):
    code = main(["capabilities", "--workdir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "features" in out and "limits:" in out


def test_validate_config_accepts_a_good_file(capsys, tmp_path):
    good = tmp_path / "settings.json"
    good.write_text('{"target_lang": "ta", "max_speed": 1.05}', encoding="utf-8")
    code = main(["validate-config", "--config", str(good), "--workdir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "configuration ok" in out


def test_validate_config_rejects_a_bad_file_without_a_traceback(capsys, tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text('{"max_speed": "fast"}', encoding="utf-8")
    code = main(["validate-config", "--config", str(bad), "--workdir", str(tmp_path / "w")])
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "Traceback" not in captured.err + captured.out


def test_jobs_list_on_an_empty_workdir(capsys, tmp_path):
    code = main(["jobs", "list", "--workdir", str(tmp_path / "w")])
    assert code == EXIT_OK
    assert "no jobs" in capsys.readouterr().out


def test_jobs_show_a_missing_job_is_an_error(capsys, tmp_path):
    code = main(["jobs", "show", "does-not-exist", "--workdir", str(tmp_path / "w")])
    assert code == EXIT_ERROR
    assert "no job" in capsys.readouterr().err


def test_jobs_list_json_shape(capsys, tmp_path):
    main(["jobs", "list", "--json", "--workdir", str(tmp_path / "w")])
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 0 and data["jobs"] == []


def test_jobs_cleanup_defaults_to_dry_run(capsys, tmp_path):
    code = main(["jobs", "cleanup", "--workdir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "would remove" in out


def test_batch_on_a_missing_folder(capsys, tmp_path):
    code = main(["batch", str(tmp_path / "nope"), "--to", "hi", "--workdir", str(tmp_path / "w")])
    assert code == EXIT_ERROR
    assert "no such file" in capsys.readouterr().err


@needs_ffmpeg
def test_batch_command_runs_a_folder(capsys, tmp_path, monkeypatch):
    from dubforge import demo

    monkeypatch.setenv("DUBFORGE_SCRIPT", demo.DEMO_SCRIPT)
    folder = tmp_path / "lessons"
    folder.mkdir()
    demo.build_clip(folder / "a.mp4")
    code = main(
        [
            "batch",
            str(folder),
            "--to",
            "hi",
            "--asr",
            "stub",
            "--translator",
            "stub",
            "--tts",
            "stub",
            "--separator",
            "none",
            "--workdir",
            str(tmp_path / "w"),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["total"] == 1
    assert data["items"][0]["status"] == "completed"
    assert code == EXIT_QC_FAILED  # stub translation is flagged, by design
