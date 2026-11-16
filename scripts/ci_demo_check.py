#!/usr/bin/env python3
"""CI gate for the offline demo — expected failure is not the same as success.

`dubforge demo` runs the whole pipeline with stub translation and a tone for a voice,
so it *must* fail QC: the check called `translated` exists precisely to stop a plumbing
test being mistaken for a finished dub. The demo therefore exits **1**, not 0.

The old CI step was `python -m dubforge demo … || true`, which cannot tell the intended
QC failure from a crash, an empty JSON file, a missing mp4, a broken filter graph or a
silent audio track. This script makes the distinction explicit:

    exit 0  the demo did exactly what it is supposed to do
    exit 1  something else happened — with a code saying what

Codes: `wrong_exit_code`, `invalid_json`, `missing_output`, `unplayable_output`,
`missing_streams`, `unexpected_qc`, `wrong_metrics`, `demo_error`.

    python scripts/ci_demo_check.py --out-dir ci-demo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

EXPECTED_EXIT = 1  # QC failure by design (stub translator)
EXPECTED_FAILURES = ["translated"]
EXPECTED_SEGMENTS = 3
MAX_OVERFLOW_S = 0.35
DURATION_TOLERANCE_S = 0.6


class DemoCheckFailed(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail


def run_demo(out_dir: Path, timeout: int = 900) -> tuple[int, str, str]:
    """Run the demo CLI. Returns (exit code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "dubforge", "demo", "--out-dir", str(out_dir), "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(out_dir: Path, timeout: int = 900) -> dict:
    """Run the demo and assert every property CI depends on. Raises DemoCheckFailed."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr = run_demo(out_dir, timeout)

    if code not in (0, EXPECTED_EXIT):
        raise DemoCheckFailed(
            "demo_error",
            f"demo exited {code} (expected {EXPECTED_EXIT} for the intended QC failure). "
            f"stderr tail: {stderr.strip().splitlines()[-1] if stderr.strip() else '(empty)'}",
        )
    if code != EXPECTED_EXIT:
        raise DemoCheckFailed(
            "wrong_exit_code",
            f"demo exited {code}; the stub pipeline must fail QC and exit {EXPECTED_EXIT}. "
            "If stub translation now passes, the `translated` QC check has been weakened.",
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DemoCheckFailed("invalid_json", f"--json produced unparseable output: {exc}") from exc

    output = Path(payload.get("output") or "")
    if not output.exists():
        raise DemoCheckFailed("missing_output", f"no output file at {output}")

    from dubforge import ffmpeg  # imported late so --help works without the package

    try:
        info = ffmpeg.probe(output)
    except ffmpeg.FFmpegError as exc:
        raise DemoCheckFailed("unplayable_output", f"{output} cannot be probed: {exc}") from exc
    if not (info.has_video and info.has_audio):
        raise DemoCheckFailed(
            "missing_streams",
            f"{output} has video={info.has_video} audio={info.has_audio}; the dub must be muxed onto the picture",
        )

    qc_data = payload.get("qc") or {}
    failures = qc_data.get("failures")
    if failures != EXPECTED_FAILURES:
        raise DemoCheckFailed(
            "unexpected_qc",
            f"QC failures were {failures!r}, expected exactly {EXPECTED_FAILURES!r} "
            "(the stub translation, and nothing else)",
        )

    metrics = qc_data.get("metrics") or {}
    problems = []
    if metrics.get("segments") != EXPECTED_SEGMENTS:
        problems.append(f"segments={metrics.get('segments')} (expected {EXPECTED_SEGMENTS})")
    if metrics.get("rendered") != EXPECTED_SEGMENTS:
        problems.append(f"rendered={metrics.get('rendered')} (expected {EXPECTED_SEGMENTS})")
    if (metrics.get("worst_overflow_s") or 0) > MAX_OVERFLOW_S:
        problems.append(f"worst_overflow_s={metrics.get('worst_overflow_s')} > {MAX_OVERFLOW_S}")
    if not 0 < (metrics.get("speech_seconds_dubbed") or 0) < 60:
        problems.append(f"speech_seconds_dubbed={metrics.get('speech_seconds_dubbed')} looks wrong")
    if (
        metrics.get("input_seconds")
        and abs(metrics["input_seconds"] - (metrics.get("output_seconds") or 0))
        > DURATION_TOLERANCE_S
    ):
        problems.append(
            f"duration drifted: {metrics['input_seconds']}s in, {metrics.get('output_seconds')}s out"
        )
    checks = {item["check"]: item for item in qc_data.get("checks", [])}
    for name in ("every_line_has_audio", "timing_fits", "speech_rate", "dub_is_audible"):
        if not checks.get(name, {}).get("ok"):
            problems.append(
                f"QC check {name} failed: {checks.get(name, {}).get('detail', 'missing')}"
            )
    if problems:
        raise DemoCheckFailed("wrong_metrics", "; ".join(problems))

    return {
        "output": str(output),
        "duration_s": round(info.duration, 2),
        "video": f"{info.width}x{info.height}@{info.fps:g}",
        "audio": f"{info.sample_rate}Hz {info.channels}ch",
        "segments": metrics.get("segments"),
        "rendered": metrics.get("rendered"),
        "worst_overflow_s": metrics.get("worst_overflow_s"),
        "qc_failures": failures,
        "exit_code": code,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the offline demo behaves as designed.")
    parser.add_argument("--out-dir", default="ci-demo")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    try:
        summary = check(Path(args.out_dir), args.timeout)
    except DemoCheckFailed as exc:
        print(f"demo check FAILED: {exc}", file=sys.stderr)
        return 1
    print("demo check passed — the pipeline works and QC correctly reports the stub translation:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
