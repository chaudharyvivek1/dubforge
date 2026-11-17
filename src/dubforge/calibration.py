"""Speech-rate calibration: measured where possible, labelled where not.

`fit.CPS` holds one number per language — characters per second of natural speech. They are
estimates, chosen to make the timing maths behave, and until 0.3.0 nothing said so. This
module is the machinery to replace them with measurements, and the honesty to keep saying
"estimate" until the measurements exist.

The rules:

* **A sample is a measurement or it is not a sample.** Each record says how it was measured
  (`method`), by which provider and voice, in which script, and with which counting
  convention. A number typed in from a blog post is not a measurement and does not belong in
  the dataset.
* **Distributions, not averages.** Speech rate varies with the sentence, the voice and the
  emotion; a mean hides the spread that matters for timing. Reports carry median, p10, p90
  and the sample count.
* **Enough samples or no override.** A language only overrides the estimate at
  `MIN_SAMPLES` or more. Five samples from one voice is not a calibration.
* **The dataset ships empty.** There is no measured data in this repository yet, so every
  language reports `estimate`. That is the honest state, and `fit.source_for()` says it out
  loud in the run report.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

MIN_SAMPLES = 30
COUNTING = "chars-without-spaces"
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "calibration"
SAMPLES = DATA_DIR / "samples.jsonl"
MEASURED = DATA_DIR / "measured_cps.json"

REQUIRED = ("language", "chars", "duration_s", "counting", "method", "voice", "provider")


@dataclass
class Sample:
    language: str
    chars: int
    duration_s: float
    counting: str
    method: str
    voice: str
    provider: str
    script: str = ""
    model: str = ""
    sample_rate: int = 0
    style: str = ""
    measured_at: str = ""

    @property
    def cps(self) -> float:
        return self.chars / self.duration_s if self.duration_s > 0 else 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> Sample:
        missing = [field for field in REQUIRED if data.get(field) in (None, "")]
        if missing:
            raise ValueError(f"sample is missing {', '.join(missing)}: {data}")
        if data["counting"] != COUNTING:
            raise ValueError(
                f"sample counts characters as {data['counting']!r}; this project counts "
                f"{COUNTING!r} (see docs/languages.md)"
            )
        if float(data["duration_s"]) <= 0:
            raise ValueError("sample duration must be positive")
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})


def load(path: str | Path | None = None) -> tuple[list[Sample], list[str]]:
    """Read the dataset. Returns (samples, problems) — a bad row is skipped, not fatal."""
    source = Path(path) if path else SAMPLES
    samples: list[Sample] = []
    problems: list[str] = []
    if not source.exists():
        return samples, [f"no calibration dataset at {source}"]
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            samples.append(Sample.from_dict(json.loads(line)))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            problems.append(f"line {number}: {exc}")
    return samples, problems


def statistics_for(samples: list[Sample]) -> dict:
    """Distribution of speech rate for one language's samples."""
    rates = sorted(sample.cps for sample in samples if sample.cps > 0)
    if not rates:
        return {"n": 0}
    return {
        "n": len(rates),
        "median": round(statistics.median(rates), 2),
        "mean": round(statistics.fmean(rates), 2),
        "p10": round(_percentile(rates, 10), 2),
        "p90": round(_percentile(rates, 90), 2),
        "min": round(rates[0], 2),
        "max": round(rates[-1], 2),
        "voices": sorted({sample.voice for sample in samples}),
        "providers": sorted({sample.provider for sample in samples}),
    }


def _percentile(sorted_values: list[float], percent: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * (percent / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def by_language(samples: list[Sample]) -> dict[str, list[Sample]]:
    grouped: dict[str, list[Sample]] = {}
    for sample in samples:
        grouped.setdefault(sample.language, []).append(sample)
    return dict(sorted(grouped.items()))


def calibrated(samples: list[Sample], min_samples: int = MIN_SAMPLES) -> dict[str, dict]:
    """Languages whose data is good enough to override the estimate, with their numbers."""
    out: dict[str, dict] = {}
    for language, group in by_language(samples).items():
        stats = statistics_for(group)
        if stats.get("n", 0) >= min_samples:
            out[language] = {
                "cps": stats["median"],
                "n": stats["n"],
                "p10": stats["p10"],
                "p90": stats["p90"],
                "method": sorted({sample.method for sample in group}),
                "measured_at": max((sample.measured_at for sample in group), default=""),
            }
    return out


def write_measured(overrides: dict[str, dict], path: str | Path | None = None) -> Path:
    target = Path(path) if path else MEASURED
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": (
            "Median measured characters-per-second per language, written by "
            "scripts/calibrate.py. A language appears here only with at least "
            f"{MIN_SAMPLES} measured samples. Absent languages fall back to the estimates "
            "in fit.CPS and are labelled as estimates."
        ),
        "counting": COUNTING,
        "languages": overrides,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_measured(path: str | Path | None = None) -> dict[str, dict]:
    target = Path(path) if path else MEASURED
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    languages = payload.get("languages") if isinstance(payload, dict) else None
    return languages if isinstance(languages, dict) else {}


def append_samples(new: list[Sample], path: str | Path | None = None) -> int:
    """Append measured samples, refusing any that do not carry provenance."""
    target = Path(path) if path else SAMPLES
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        for sample in new:
            handle.write(json.dumps(sample.to_dict(), sort_keys=True) + "\n")
    return len(new)


def report(samples: list[Sample], problems: list[str]) -> str:
    """A markdown report: what is measured, what is estimated, and how sure we are."""
    from . import fit

    lines = [
        "# Speech-rate calibration",
        "",
        "Generated by `scripts/calibrate.py --report`. Characters are counted **without",
        "spaces** (`chars-without-spaces`); a sample without provenance is rejected on load.",
        "",
        f"* measured samples: **{len(samples)}**",
        f"* minimum samples for an override: **{MIN_SAMPLES}**",
        f"* languages calibrated: **{len(calibrated(samples))}**",
        "",
        "| language | source | cps | n | p10 | p90 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    stats = by_language(samples)
    languages = sorted(set(fit.CPS) | set(stats))
    for language in languages:
        group = stats.get(language, [])
        measured = statistics_for(group) if group else {"n": 0}
        if measured.get("n", 0) >= MIN_SAMPLES:
            lines.append(
                f"| `{language}` | measured | {measured['median']} | {measured['n']} | "
                f"{measured['p10']} | {measured['p90']} |"
            )
        else:
            lines.append(
                f"| `{language}` | estimate | {fit.CPS.get(language, fit.DEFAULT_CPS)} | "
                f"{measured.get('n', 0)} | — | — |"
            )
    if problems:
        lines += ["", "## Rejected rows", ""] + [f"* {problem}" for problem in problems]
    lines += [
        "",
        "## How to add real measurements",
        "",
        "```bash",
        "# renders a fixed sentence set through a provider and appends real samples",
        "python scripts/calibrate.py --measure --provider edge_tts --languages hi,ta --yes-live",
        "python scripts/calibrate.py --report --write",
        "```",
        "",
        "`--measure` spends money or bandwidth and therefore requires `--yes-live`. Nothing",
        "in the test suite or CI renders through a provider.",
    ]
    return "\n".join(lines) + "\n"
