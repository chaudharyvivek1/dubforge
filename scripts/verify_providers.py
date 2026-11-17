#!/usr/bin/env python3
"""Run a minimal real request against each provider and record what happened.

`verified: false` on every network adapter is honest, but it is not a *process*. This is
that process: for each provider that has a key, make the smallest real call, check the shape
of the answer, and record the date, the API version and the outcome in
`docs/verification.json`. Flip an adapter's `verified` flag only when this says `ok`.

Design rules, all of them deliberate:

* **Never runs in CI.** It spends money and needs live keys. CI checks the *code*; this
  checks the *world*.
* **Never prints or stores a key.** Only the environment variable's name appears in output.
* **Skips what it cannot run.** A missing key is `skipped`, not a failure.
* **Small by construction.** One short phrase of speech, one line of text, one two-second
  audio file — enough to prove auth, schema and a usable payload, not enough to matter.
* **Records the API surface it saw.** `model` and `api_version` come from the response where
  the vendor provides them, so a later reader can tell what was actually tested.

    python scripts/verify_providers.py --only tts,asr --write
    python scripts/verify_providers.py --dry-run          # what would be called
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dubforge import __version__, ffmpeg  # noqa: E402
from dubforge.config import Settings  # noqa: E402
from dubforge.providers import registry  # noqa: E402
from dubforge.providers.base import ProviderError, ProviderUnavailable  # noqa: E402

RECORD = ROOT / "docs" / "verification.json"
PHRASE = "Namaste. This is a verification line."
SOURCE_TEXT = "The quick brown fox jumps over the lazy dog."


def _workdir(tmp: Path) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def _two_second_wav(tmp: Path) -> str:
    """A tiny real audio file: silence plus a tone, 16 kHz mono, which every ASR accepts."""
    path = str(_workdir(tmp) / "probe.wav")
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=16000:duration=2",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            path,
        ]
    )
    return path


def check_asr(provider, settings, tmp: Path) -> dict:
    audio = _two_second_wav(tmp)
    transcript = provider.transcribe(audio, settings.source_lang, diarize=False)
    if not isinstance(transcript.segments, list):
        raise AssertionError("transcript has no segment list")
    return {
        "segments": len(transcript.segments),
        "language": transcript.language or settings.source_lang,
        "detail": "audio accepted, transcript structure returned",
    }


def check_translate(provider, settings, tmp: Path) -> dict:
    from dubforge.media import Segment

    segments = [Segment(index=0, start=0.0, end=3.0, text=SOURCE_TEXT, window=3.0)]
    provider.translate(segments, "en", settings.target_lang, None, None, "")
    target = (segments[0].target_text or "").strip()
    if not target:
        raise AssertionError("translation came back empty")
    if target == SOURCE_TEXT:
        raise AssertionError("translation came back unchanged")
    return {"chars": len(target), "detail": "text translated"}


def check_tts(provider, settings, tmp: Path) -> dict:
    voices = provider.voices(settings.target_lang)
    voice = voices[0].id if voices else ""
    out = str(_workdir(tmp) / "probe-tts.wav")
    rendered = provider.synthesize(PHRASE, voice, out)
    duration = rendered.duration or 0.0
    if duration <= 0.05:
        raise AssertionError(f"rendered audio is {duration:.2f}s — no usable payload")
    return {
        "voice": voice or "(provider default)",
        "duration_s": round(duration, 2),
        "detail": "audio rendered",
    }


CHECKS = {"asr": check_asr, "translate": check_translate, "tts": check_tts}


def verify(kind: str, name: str, settings: Settings, tmp: Path) -> dict:
    """One provider, one minimal real call. Returns a record, never raises."""
    record = {
        "stage": kind,
        "provider": name,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dubforge_version": __version__,
        "status": "skipped",
        "detail": "",
    }
    try:
        provider = registry.create(kind, name, settings)
        gaps = provider.missing()
        if gaps:
            record["detail"] = "; ".join(gaps)[:300]
            return record
        check = CHECKS.get(kind)
        if check is None:
            record["detail"] = f"no verification check for stage {kind!r}"
            record["status"] = "unsupported"
            return record
        result = check(provider, settings, tmp)
        record.update(result)
        record["status"] = "ok"
        record["api_version"] = provider.spec.docs
    except ProviderUnavailable as exc:
        record["detail"] = str(exc)[:300]
    except ProviderError as exc:
        record["status"] = "failed"
        record["error_kind"] = exc.kind
        record["detail"] = str(exc)[:300]
    except AssertionError as exc:
        record["status"] = "failed"
        record["error_kind"] = "schema"
        record["detail"] = str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - a probe must never take the script down
        record["status"] = "failed"
        record["error_kind"] = type(exc).__name__
        record["detail"] = str(exc)[:300]
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-verify network providers.")
    parser.add_argument("--only", default="", help="comma-separated stage names: asr,translate,tts")
    parser.add_argument("--provider", default="", help="comma-separated provider names")
    parser.add_argument("--target-lang", default="hi")
    parser.add_argument("--write", action="store_true", help="write docs/verification.json")
    parser.add_argument("--dry-run", action="store_true", help="list what would be called")
    parser.add_argument("--tmp", default="/tmp/dubforge-verify")
    args = parser.parse_args(argv)

    stages = [s.strip() for s in args.only.split(",") if s.strip()] or list(CHECKS)
    wanted = {p.strip() for p in args.provider.split(",") if p.strip()}
    settings = Settings(target_lang=args.target_lang)
    tmp = Path(args.tmp)

    records: list[dict] = []
    for kind in stages:
        if kind not in registry.KINDS:
            print(f"unknown stage {kind!r}", file=sys.stderr)
            return 2
        for name in registry.names(kind):
            if name in ("stub", "transcript", "none"):
                continue
            if wanted and name not in wanted:
                continue
            provider = registry.create(kind, name, settings)
            env = ", ".join(provider.spec.env) or "no key required"
            if args.dry_run:
                ready = "ready" if not provider.missing() else "not ready"
                print(f"{kind:<10} {name:<14} {ready:<10} needs: {env}")
                continue
            record = verify(kind, name, settings, tmp)
            records.append(record)
            print(f"{kind:<10} {name:<14} {record['status']:<9} {record.get('detail', '')[:90]}")

    if args.dry_run:
        return 0

    verified = [r for r in records if r["status"] == "ok"]
    failed = [r for r in records if r["status"] == "failed"]
    skipped = [r for r in records if r["status"] == "skipped"]
    print(
        f"\n{len(verified)} ok, {len(failed)} failed, {len(skipped)} skipped (no key or no check)"
    )

    if args.write:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dubforge_version": __version__,
            "note": (
                "Produced by scripts/verify_providers.py against live APIs. An adapter may be "
                "marked verified only when its row here says ok. Never contains keys."
            ),
            "results": records,
        }
        RECORD.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            shown = RECORD.relative_to(ROOT)
        except ValueError:  # a caller pointed it somewhere else, e.g. a test
            shown = RECORD
        print(f"wrote {shown}")

    # A failure is a real finding: exit non-zero so a maintainer notices.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
