"""The benchmark script: real media, real numbers, no CI flakiness."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import benchmark  # noqa: E402
from conftest import needs_ffmpeg  # noqa: E402


def test_unknown_sizes_are_refused(capsys):
    code = benchmark.main(["--sizes", "enormous"])
    assert code == 2
    assert "unknown size" in capsys.readouterr().err


@needs_ffmpeg
def test_medium_media_is_really_longer_than_small(tmp_path):
    small = benchmark.build_media("small", tmp_path / "m")
    medium = benchmark.build_media("medium", tmp_path / "m")
    from dubforge import ffmpeg

    assert ffmpeg.probe(medium).duration > ffmpeg.probe(small).duration * 3


@needs_ffmpeg
def test_a_small_run_reports_the_numbers_it_measured(tmp_path, capsys):
    code = benchmark.main(["--sizes", "small", "--json", "--tmp", str(tmp_path / "bench")])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["size"] == "small"
    assert row["media_seconds"] > 5
    assert row["dub_s"] > 0
    assert row["peak_rss_mb"] > 0
    assert row["work_files"] > 0
    assert row["qc_checks"] >= 20, "a single-track source produces twenty checks"
    assert payload["note"].startswith("Stub providers")
