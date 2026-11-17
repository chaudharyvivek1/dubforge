#!/usr/bin/env python3
"""Measure before optimising: a repeatable benchmark on small, medium and large media.

The numbers that matter for this project are not throughput — ffmpeg dominates — but the
overhead the engine adds around it: how long planning takes, how much memory a run holds,
how many temporary files a job leaves behind, and what the job layer costs on top of a
direct call.

Everything here uses the stub providers, so it measures **this code**, not a vendor's
latency. That is the point: a vendor benchmark ages in a week, an engine benchmark does not.

    python scripts/benchmark.py                     # all three sizes
    python scripts/benchmark.py --sizes small --json
    python scripts/benchmark.py --keep              # leave the work directories for inspection

Not part of CI: it takes minutes, and a shared runner's timing is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dubforge import __version__, demo, ffmpeg, pipeline  # noqa: E402
from dubforge.config import Settings  # noqa: E402
from dubforge.jobs import JobRunner  # noqa: E402

# seconds of media per size, and how many copies of the demo clip that needs
SIZES = {"small": 1, "medium": 6, "large": 20}  # × 9.2 s ≈ 9 s, 55 s, 184 s


def build_media(size: str, workdir: Path) -> Path:
    """A real mp4 of the requested size, built from the demo clip and stream-copied."""
    workdir.mkdir(parents=True, exist_ok=True)
    base = workdir / "base.mp4"
    if not base.exists():
        demo.build_clip(base)
    target = workdir / f"{size}.mp4"
    if target.exists():
        return target
    loops = SIZES[size] - 1
    if loops <= 0:
        shutil.copy(base, target)
        return target
    ffmpeg.run(["-stream_loop", str(loops), "-i", str(base), "-c", "copy", str(target)])
    return target


def _rss_mb() -> float | None:
    """Peak resident memory, or None where the platform will not say.

    `resource` is POSIX-only, so Windows goes through psapi. Returning None is a valid
    answer: a benchmark that invents a number is worse than one that says "n/a".
    """
    try:
        import resource
    except ImportError:
        return _rss_mb_windows()
    # ru_maxrss is KiB on Linux and bytes on macOS.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _rss_mb_windows() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if ok:
            return round(counters.PeakWorkingSetSize / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - an unavailable metric is not a failure
        return None
    return None


def measure(size: str, root: Path) -> dict:
    media = build_media(size, root / "media")
    info = ffmpeg.probe(media)
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(root / f"work-{size}"),
        cache=False,
    )

    started = time.perf_counter()
    plan = pipeline.plan(media, settings)
    plan_ms = round((time.perf_counter() - started) * 1000, 1)

    started = time.perf_counter()
    result = pipeline.dub(media, settings, output=root / f"{size}.hi.mp4")
    dub_s = round(time.perf_counter() - started, 2)

    job_started = time.perf_counter()
    runner = JobRunner(settings)
    try:
        job = runner.submit(media, settings, root / f"{size}.job.mp4")
        final = runner.wait(job.id, timeout=3600)
    finally:
        runner.shutdown(wait=True)
    job_s = round(time.perf_counter() - job_started, 2)

    job_dir = Path(result.workdir)
    temp_files = sum(1 for _ in job_dir.rglob("*") if _.is_file()) if job_dir.exists() else 0

    return {
        "size": size,
        "media_seconds": round(info.duration, 1),
        "media_mb": round(media.stat().st_size / 1e6, 1),
        "segments": int(result.qc.get("metrics", {}).get("segments", 0) or 0),
        "plan_ms": plan_ms,
        "dub_s": dub_s,
        "job_total_s": job_s,
        "job_overhead_s": round(job_s - dub_s, 2),
        "peak_rss_mb": _rss_mb(),
        "work_files": temp_files,
        "providers": plan.providers,
        "qc_checks": len(result.qc["checks"]),
        "qc_failures": result.qc["failures"],
        "job_status": final.status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the engine on three media sizes.")
    parser.add_argument("--sizes", default="small,medium,large")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temporary work tree")
    parser.add_argument("--tmp", default="")
    args = parser.parse_args(argv)

    sizes = [s.strip() for s in args.sizes.split(",") if s.strip()]
    unknown = [s for s in sizes if s not in SIZES]
    if unknown:
        print(f"unknown size(s): {unknown}; choose from {sorted(SIZES)}", file=sys.stderr)
        return 2

    root = Path(args.tmp) if args.tmp else Path(tempfile.mkdtemp(prefix="dubforge-bench-"))
    rows = []
    try:
        for size in sizes:
            row = measure(size, root)
            rows.append(row)
            if not args.json:
                print(
                    f"{size:<7} {row['media_seconds']:>7.1f}s  {row['media_mb']:>6.1f}MB  "
                    f"plan {row['plan_ms']:>6.1f}ms  dub {row['dub_s']:>6.2f}s  "
                    f"job +{row['job_overhead_s']:>5.2f}s  "
                    f"rss {(row['peak_rss_mb'] if row['peak_rss_mb'] is not None else 'n/a'):>6}MB  "
                    f"files {row['work_files']:>4}"
                )
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dubforge_version": __version__,
        "note": (
            "Stub providers: this measures the engine, not vendor latency. Numbers from a "
            "shared CI runner are not a measurement, which is why this does not run in CI."
        ),
        "results": rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    elif rows:
        print(
            f"\n{len(rows)} size(s) measured on {sys.platform}; work tree: "
            f"{root if args.keep else 'removed'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
