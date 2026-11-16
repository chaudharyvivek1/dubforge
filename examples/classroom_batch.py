#!/usr/bin/env python3
"""Dub a folder of lessons and keep a QC ledger.

This is the script a school actually needs: run it overnight over a term's videos,
never overwrite a good output, and end up with a CSV that says which files are safe
to show students.

    export OPENAI_API_KEY=…  ELEVENLABS_API_KEY=…
    python examples/classroom_batch.py lessons/ --to hi --out dubbed/

Exit code is 0 only if every file passed QC, so it can gate a publish step.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from dubforge import Settings, dub, plan

MEDIA = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".flac"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-dub a folder of lessons with QC.")
    parser.add_argument("folder", help="folder of videos")
    parser.add_argument("--to", required=True, help="target language, e.g. hi")
    parser.add_argument("--from", dest="source_lang", default="auto")
    parser.add_argument("--out", default="dubbed", help="output folder")
    parser.add_argument("--asr", default="auto")
    parser.add_argument("--translator", default="auto")
    parser.add_argument("--tts", default="auto")
    parser.add_argument("--voice-pool", default="", help="comma-separated voice ids")
    parser.add_argument("--style", default="warm, unhurried teacher")
    parser.add_argument("--context", default="", help="what the lessons are about")
    parser.add_argument("--workdir", default="dubforge-work")
    parser.add_argument("--ledger", default="dub-ledger.csv")
    parser.add_argument("--plan-only", action="store_true", help="estimate cost, render nothing")
    parser.add_argument("--force", action="store_true", help="re-dub even if the output exists")
    args = parser.parse_args()

    source_dir = Path(args.folder)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in source_dir.rglob("*") if p.suffix.lower() in MEDIA)
    if not files:
        print(f"no media files in {source_dir}", file=sys.stderr)
        return 2

    settings = Settings.from_env(
        target_lang=args.to,
        source_lang=args.source_lang,
        asr=args.asr,
        translator=args.translator,
        tts=args.tts,
        voice_pool=[v.strip() for v in args.voice_pool.split(",") if v.strip()],
        style=args.style,
        context=args.context or None,
        workdir=args.workdir,
    )

    rows = []
    failures = 0
    for path in files:
        target = out_dir / f"{path.stem}.{args.to}{'.mp4' if path.suffix.lower() != '.mp3' else '.m4a'}"

        if args.plan_only:
            p = plan(path, settings)
            rows.append({"file": path.name, "minutes": p.estimates["minutes"],
                         "usd": p.estimates["usd"], "ready": p.ready,
                         "problems": "; ".join(p.problems), "qc": "", "output": ""})
            print(f"plan  {path.name:<40} {p.estimates['minutes']:>6.1f} min  "
                  f"${p.estimates['usd'] if p.estimates['usd'] is not None else '?'}  "
                  f"{'ready' if p.ready else 'BLOCKED'}")
            continue

        if target.exists() and not args.force:
            print(f"skip  {path.name} (already dubbed)")
            rows.append({"file": path.name, "minutes": "", "usd": "", "ready": True,
                         "problems": "", "qc": "existing", "output": str(target)})
            continue

        print(f"dub   {path.name} → {target.name}")
        try:
            result = dub(path, settings, output=target, log=lambda m: print(f"      {m}"))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            failures += 1
            print(f"      FAILED: {exc}")
            rows.append({"file": path.name, "minutes": "", "usd": "", "ready": False,
                         "problems": str(exc)[:300], "qc": "error", "output": ""})
            continue

        if not result.ok:
            failures += 1
            print(f"      QC FAILED: {', '.join(result.qc.get('failures', []))}")
        else:
            print(f"      QC ok — {result.qc['metrics'].get('rendered')} lines, "
                  f"worst overrun {result.qc['metrics'].get('worst_overflow_s')}s")
        rows.append({
            "file": path.name,
            "minutes": result.plan["estimates"]["minutes"],
            "usd": result.plan["estimates"]["usd"],
            "ready": True,
            "problems": "; ".join(result.warnings)[:300],
            "qc": "pass" if result.ok else "fail",
            "output": result.output,
        })

    with open(args.ledger, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "minutes", "usd", "ready", "problems", "qc", "output"])
        writer.writeheader()
        writer.writerows(rows)

    total_usd = sum(r["usd"] for r in rows if isinstance(r["usd"], (int, float)))
    print(f"\n{len(rows)} file(s), {failures} problem(s), ~${total_usd:.2f}")
    print(f"ledger: {args.ledger}")
    if any(r["qc"] == "fail" for r in rows):
        print("Files that failed QC are listed in the ledger — read their qc.json before publishing.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
