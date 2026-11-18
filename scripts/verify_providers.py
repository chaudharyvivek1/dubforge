#!/usr/bin/env python3
"""Live-verify network providers, and record what happened.

This is the script form of `dubforge verify`; the implementation lives in
`src/dubforge/verify.py` so it can be planned, imported, summarised in `doctor` and tested
without a key. Kept because a maintainer with keys should be able to run one file.

    python scripts/verify_providers.py --dry-run          # what would be called, and why not
    python scripts/verify_providers.py --only tts --provider elevenlabs
    python scripts/verify_providers.py --write            # record the result

It refuses to make requests without `--yes-live`, never prints or stores a key, and treats a
missing key as `skipped` rather than `failed`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dubforge import verify  # noqa: E402
from dubforge.config import Settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-verify network providers.")
    parser.add_argument("--only", default="", help="comma-separated stages: asr,translate,tts")
    parser.add_argument("--provider", default="", help="comma-separated provider names")
    parser.add_argument("--target-lang", default="hi")
    parser.add_argument("--write", action="store_true", help="write docs/verification.json")
    parser.add_argument("--dry-run", action="store_true", help="list what would be called")
    parser.add_argument(
        "--yes-live",
        action="store_true",
        help="required to actually make the requests (spends money)",
    )
    parser.add_argument("--tmp", default="/tmp/dubforge-verify")
    args = parser.parse_args(argv)

    settings = Settings(target_lang=args.target_lang)
    if args.dry_run:
        print(verify.render_plan(verify.plan(settings)))
        return 0
    try:
        payload = verify.run(
            settings,
            yes_live=args.yes_live,
            only=[s.strip() for s in args.only.split(",") if s.strip()] or None,
            providers=[s.strip() for s in args.provider.split(",") if s.strip()] or None,
            write=args.write,
            tmp=Path(args.tmp),
            emit=print,
        )
    except verify.VerifyError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    ok = sum(1 for row in payload["results"] if row["status"] == "ok")
    failed = sum(1 for row in payload["results"] if row["status"] == "failed")
    skipped = sum(1 for row in payload["results"] if row["status"] == "skipped")
    print(f"\n{ok} ok, {failed} failed, {skipped} skipped (no key or no check)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
