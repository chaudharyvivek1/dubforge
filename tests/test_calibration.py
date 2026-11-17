"""Calibration: distributions, provenance, and a refusal to call an estimate a measurement."""

import json
from pathlib import Path

import pytest

from dubforge import calibration, fit


def sample(language="hi", chars=100, duration=8.0, voice="v1", method="tts-render") -> dict:
    return {
        "language": language,
        "chars": chars,
        "duration_s": duration,
        "counting": calibration.COUNTING,
        "method": method,
        "voice": voice,
        "provider": "test",
    }


def write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "samples.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------- provenance


def test_a_sample_without_provenance_is_rejected():
    row = sample()
    del row["voice"]
    with pytest.raises(ValueError, match="missing voice"):
        calibration.Sample.from_dict(row)


def test_a_sample_with_another_counting_convention_is_rejected():
    row = sample()
    row["counting"] = "chars-with-spaces"
    with pytest.raises(ValueError, match="counts characters"):
        calibration.Sample.from_dict(row)


def test_a_zero_duration_is_rejected():
    with pytest.raises(ValueError, match="duration must be positive"):
        calibration.Sample.from_dict(sample(duration=0))


def test_a_bad_row_is_reported_and_skipped_not_fatal(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text(
        json.dumps(sample()) + "\n" + "{not json}\n" + json.dumps({"language": "hi"}) + "\n",
        encoding="utf-8",
    )
    samples, problems = calibration.load(path)
    assert len(samples) == 1
    assert len(problems) == 2


# ------------------------------------------------------------------ distribution


def test_statistics_are_a_distribution_not_an_average():
    rows = [
        calibration.Sample.from_dict(sample(chars=chars, duration=10.0))
        for chars in (80, 90, 100, 110, 200)
    ]
    stats = calibration.statistics_for(rows)
    assert stats["n"] == 5
    assert stats["median"] == pytest.approx(10.0)
    assert stats["p10"] < stats["median"] < stats["p90"]
    assert stats["min"] == 8.0 and stats["max"] == 20.0
    assert stats["mean"] == pytest.approx(11.6)


def test_one_sample_has_no_spread():
    rows = [calibration.Sample.from_dict(sample())]
    stats = calibration.statistics_for(rows)
    assert stats["n"] == 1 and stats["p10"] == stats["p90"] == stats["median"]


def test_percentiles_interpolate_between_ranks():
    values = [1.0, 2.0, 3.0, 4.0]
    assert calibration._percentile(values, 0) == 1.0
    assert calibration._percentile(values, 100) == 4.0
    assert calibration._percentile(values, 50) == pytest.approx(2.5)


# --------------------------------------------------------------- the override gate


def test_a_language_below_the_threshold_does_not_override_the_estimate(tmp_path, monkeypatch):
    rows = [sample(chars=100, duration=8.0) for _ in range(5)]
    samples, _ = calibration.load(write(tmp_path, rows))
    assert calibration.calibrated(samples, min_samples=30) == {}


def test_a_language_at_the_threshold_overrides_with_its_median(tmp_path):
    rows = [sample(chars=100, duration=8.0) for _ in range(28)]
    rows += [sample(chars=100, duration=10.0) for _ in range(2)]  # median 12.5 cps
    samples, _ = calibration.load(write(tmp_path, rows))
    overrides = calibration.calibrated(samples, min_samples=30)
    assert overrides["hi"]["cps"] == pytest.approx(12.5)
    assert overrides["hi"]["n"] == 30


def test_two_languages_are_calibrated_independently(tmp_path):
    rows = [sample(language="hi", chars=100, duration=8.0) for _ in range(30)]
    rows += [sample(language="ta", chars=100, duration=12.0) for _ in range(30)]
    samples, _ = calibration.load(write(tmp_path, rows))
    overrides = calibration.calibrated(samples, min_samples=30)
    assert overrides["hi"]["cps"] == pytest.approx(12.5)
    assert overrides["ta"]["cps"] == pytest.approx(8.33, abs=0.01)


# ----------------------------------------------------------- fit integration


def test_fit_reports_an_estimate_when_nothing_is_measured(monkeypatch):
    monkeypatch.setattr(fit, "_MEASURED", {})
    assert fit.source_for("hi") == "estimate"
    assert fit.cps_for("hi") == fit.CPS["hi"]


def test_fit_uses_a_measured_value_and_says_so(monkeypatch):
    monkeypatch.setattr(fit, "_MEASURED", {"hi": {"cps": 11.0, "n": 40}})
    assert fit.cps_for("hi") == 11.0
    assert fit.source_for("hi") == "measured(n=40)"


def test_the_measured_file_round_trips(tmp_path):
    path = tmp_path / "measured_cps.json"
    calibration.write_measured({"hi": {"cps": 12.0, "n": 31, "p10": 11.0, "p90": 13.0}}, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["languages"]["hi"]["cps"] == 12.0
    assert payload["counting"] == calibration.COUNTING
    assert calibration.load_measured(path)["hi"]["n"] == 31


def test_a_missing_or_corrupt_measured_file_means_estimates(tmp_path):
    assert calibration.load_measured(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert calibration.load_measured(bad) == {}


# ------------------------------------------------------------------- the report


def test_the_report_separates_measured_from_estimated(tmp_path):
    rows = [sample(language="hi", chars=100, duration=8.0) for _ in range(30)]
    samples, problems = calibration.load(write(tmp_path, rows))
    text = calibration.report(samples, problems)
    assert "| `hi` | measured |" in text
    assert "| `ta` | estimate |" in text
    assert "minimum samples for an override: **30**" in text
    assert not problems


def test_the_report_lists_rejected_rows(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text("{oops\n", encoding="utf-8")
    samples, problems = calibration.load(path)
    text = calibration.report(samples, problems)
    assert "## Rejected rows" in text
    assert "line 1" in text


def test_the_shipped_dataset_is_empty_and_honest():
    """Nothing has been measured yet, and the repository must not pretend otherwise."""
    samples, problems = calibration.load()
    assert samples == []
    assert problems == []
    assert calibration.load_measured() == {}
    assert fit.source_for("hi") == "estimate"


# ----------------------------------------------------------------- the harness


def test_measuring_without_opt_in_is_refused(capsys, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import calibrate

    monkeypatch.delenv("EDGE_TTS", raising=False)
    code = calibrate.main(["--measure", "--provider", "edge_tts"])
    assert code == 2
    assert "refusing to make live provider requests" in capsys.readouterr().err


def test_the_counting_convention_is_defined_once():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import calibrate

    assert calibrate.count_chars("hello world") == 10
    # Devanagari: "नमस्ते" is 6 code points, "दुनिया" 6, plus one combining mark in नमस्ते
    assert calibrate.count_chars("नमस्ते दुनिया") == len("नमस्तेदुनिया")
    assert calibrate.count_chars("") == 0
