"""`dubforge` on the command line.

Everything the MCP server can do is here too, because the first thing anyone does
with a new tool is try to run it by hand, and the second is put it in a cron job.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from . import __version__, batch, capabilities, demo, doctor, ffmpeg, limits, pipeline, qc, srt
from .config import ConfigError, Settings
from .jobs import JobError, JobStore
from .media import Transcript
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable

EXIT_OK, EXIT_QC_FAILED, EXIT_ERROR = 0, 1, 2


def _utf8_streams() -> None:
    """Make the console able to print what we print.

    A Windows console defaults to a legacy code page, and a transcript or a QC report
    containing a single character outside it (`→`, Devanagari, Tamil) raises
    `UnicodeEncodeError` and kills the command — with exit code 1, which is also the
    code for "QC failed". Reconfiguring with `errors="replace"` cannot fail loudly.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def _emit_json(payload) -> None:
    """JSON on stdout that no console encoding can break.

    `ensure_ascii=True` keeps machine output pure ASCII: `\u0915` is valid JSON and
    every consumer decodes it, while a raw Devanagari character would crash a legacy
    console mid-write. Human-readable output still prints in full UTF-8.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    sys.stdout.flush()


def _settings(args) -> Settings:
    overrides = {}
    for name in (
        "source_lang",
        "target_lang",
        "asr",
        "translator",
        "tts",
        "separator",
        "lipsync",
        "max_speed",
        "rewrite_rounds",
        "bed_gain_db",
        "voice_gain_db",
        "loudness_lufs",
        "style",
        "emotion",
        "workdir",
        "asr_model",
        "translate_model",
        "tts_model",
        "transcript",
        "script",
        "context",
        "audio_bitrate",
    ):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    if getattr(args, "no_background", False):
        overrides["keep_background"] = False
    if getattr(args, "no_duck", False):
        overrides["duck"] = False
    if getattr(args, "no_cache", False):
        overrides["cache"] = False
    if getattr(args, "dry_run", False):
        overrides["dry_run"] = True
    if getattr(args, "voice_pool", None):
        overrides["voice_pool"] = [v.strip() for v in args.voice_pool.split(",") if v.strip()]
    if getattr(args, "voice_map", None):
        overrides["voice_map"] = _parse_pairs(args.voice_map)
    if getattr(args, "glossary", None):
        overrides["glossary"] = _parse_pairs(args.glossary)
    return Settings.load(getattr(args, "config", None), **overrides)


def _parse_pairs(raw: str) -> dict:
    """`A=B,C=D` (or `A:B`) → {"A": "B"}. Quotes and commas in values are tolerated."""
    out: dict[str, str] = {}
    for chunk in _split_top_level(raw):
        for sep in ("=", ":"):
            if sep in chunk:
                key, value = chunk.split(sep, 1)
                if key.strip():
                    out[key.strip().strip('"')] = value.strip().strip('"')
                break
    return out


def _split_top_level(raw: str) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


# --------------------------------------------------------------------------- commands


def cmd_doctor(args) -> int:
    settings = _settings(args)
    if args.json:
        _emit_json(doctor.checks(settings))
    else:
        print(doctor.text(settings))
    return EXIT_OK


def cmd_providers(args) -> int:
    settings = _settings(args)
    rows = registry.all_specs(settings)
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.json:
        _emit_json(rows)
        return EXIT_OK
    current = None
    for row in rows:
        if row["kind"] != current:
            current = row["kind"]
            print(f"\n{current.upper()}")
        flag = "ready  " if row["available"] else "not set"
        mark = "verified" if row["verified"] else "UNVERIFIED"
        print(f"  {row['name']:<16} {flag}  {mark:<10} {row['title']}")
        if row["quality"]:
            print(f"      {row['quality']}")
        if row["missing"]:
            print(f"      missing: {'; '.join(row['missing'])}")
    print(
        "\nUNVERIFIED means the adapter is written against the vendor's documentation and has not been"
    )
    print(
        "exercised against the live API in this release. Check its first output before a large batch."
    )
    return EXIT_OK


def cmd_voices(args) -> int:
    settings = _settings(args)
    provider = registry.resolve("tts", settings, args.tts)
    voices = provider.voices(args.lang or settings.target_lang)
    if args.json:
        _emit_json([v.to_dict() for v in voices])
        return EXIT_OK
    if not voices:
        print(
            f"{provider.name} returned no voice list (it may need a key, a package or a language)."
        )
        return EXIT_OK
    for voice in voices:
        print(
            f"{voice.id:<28} {voice.name:<24} {voice.language:<8} {voice.gender:<8} {voice.style}"
        )
    return EXIT_OK


def cmd_plan(args) -> int:
    settings = _settings(args)
    plan = pipeline.plan(args.input, settings)
    if args.json:
        _emit_json(plan.to_dict())
    else:
        print(plan.text())
    return EXIT_OK if plan.ready else EXIT_ERROR


def cmd_dub(args) -> int:
    settings = _settings(args)
    try:
        result = pipeline.dub(
            args.input, settings, output=args.out, log=None if args.json else print
        )
    except (pipeline.DubError, ProviderError, ProviderUnavailable, ffmpeg.FFmpegError) as exc:
        print(f"dubforge: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        _emit_json(result.to_dict())
    else:
        print()
        print(qc.summary(result.qc))
        print()
        print(f"output      {result.output}")
        print(f"audio       {result.audio}")
        print(f"transcript  {result.transcript_json}")
        for name, path in result.subtitles.items():
            print(f"{name:<11} {path}")
        print(f"report      {result.report}")
        for warning in result.warnings:
            print(f"warning     {warning}")
    return EXIT_OK if result.ok else EXIT_QC_FAILED


def cmd_demo(args) -> int:
    payload = demo.run(args.out_dir, target_lang=args.to, log=None if args.json else print)
    if args.json:
        _emit_json(payload)
        return EXIT_OK if payload["qc_ok"] else EXIT_QC_FAILED
    print()
    print(qc.summary(payload["qc"]))
    print()
    print(f"source      {payload['source']}")
    print(f"dubbed      {payload['output']}")
    print(f"report      {payload['report']}")
    print()
    print(payload["note"])
    return EXIT_OK if payload["qc_ok"] else EXIT_QC_FAILED


def cmd_srt(args) -> int:
    """Convert a dubforge transcript dump into subtitles (useful for review)."""
    transcript = Transcript.load(args.input)
    out = srt.write_all(transcript.segments, args.out_dir, args.stem or "dub")
    _emit_json(out)
    return EXIT_OK


def cmd_capabilities(args) -> int:
    settings = _settings(args)
    if args.json:
        _emit_json(capabilities.snapshot(settings))
    else:
        print(capabilities.text(settings))
    return EXIT_OK


def cmd_validate_config(args) -> int:
    """Check a settings file and the environment without starting any work."""
    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"dubforge: configuration error\n{exc}", file=sys.stderr)
        return EXIT_ERROR
    problems = settings.validate() + list(settings.env_warnings)
    payload = {
        "ok": not problems,
        "problems": problems,
        "config": args.config or None,
        "effective": {
            "target_lang": settings.target_lang,
            "source_lang": settings.source_lang,
            "max_speed": settings.max_speed,
            "keep_background": settings.keep_background,
            "fallback": settings.fallback,
            "limits": limits.describe(settings),
        },
    }
    if args.json:
        _emit_json(payload)
    elif problems:
        print("configuration problems:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("configuration ok")
        print(limits.summary(settings))
    return EXIT_OK if not problems else EXIT_ERROR


def cmd_jobs(args) -> int:
    settings = _settings(args)
    store = JobStore(settings.workdir)
    action = args.action

    if action == "list":
        jobs, total = store.list(status=args.status or None, limit=args.limit, offset=args.offset)
        if args.json:
            _emit_json(
                {"total": total, "returned": len(jobs), "jobs": [job.summary() for job in jobs]}
            )
            return EXIT_OK
        if not jobs:
            print(f"no jobs in {settings.workdir}")
            return EXIT_OK
        print(f"{'job':<28} {'status':<11} {'qc':<5} {'progress':>8}  target  source")
        for job in jobs:
            qc_flag = "—" if job.qc_ok is None else ("pass" if job.qc_ok else "FAIL")
            print(
                f"{job.id:<28} {job.status:<11} {qc_flag:<5} {job.progress:>7.0%}  "
                f"{job.target_lang:<7} {Path(job.source).name}"
            )
        print(f"\n{len(jobs)} of {total} job(s) — --offset to page")
        return EXIT_OK

    if action in ("show", "cancel", "retry"):
        if not args.job_id:
            print(f"dubforge: jobs {action} needs a job id", file=sys.stderr)
            return EXIT_ERROR
        if action == "show":
            job = store.get(args.job_id)
            if job is None:
                print(f"dubforge: no job {args.job_id!r}", file=sys.stderr)
                return EXIT_ERROR
            if args.json:
                _emit_json(
                    {
                        "job": job.summary(),
                        "result": {
                            "output": job.output,
                            "audio": job.audio,
                            "qc": job.qc,
                            "subtitles": job.subtitles,
                            "providers_used": job.providers_used,
                            "warnings": job.warnings,
                            "error": job.error,
                        },
                    }
                )
                return EXIT_OK if job.status == "completed" and job.qc_ok else EXIT_QC_FAILED
            print(json.dumps(job.summary(), indent=2))
            if job.error:
                print(f"error: {job.error}")
            if job.qc:
                print()
                print(qc.summary(job.qc))
            return EXIT_OK if job.status == "completed" and job.qc_ok else EXIT_QC_FAILED

        try:
            if action == "cancel":
                job = store.request_cancel(args.job_id)
                print(f"{job.id}: {job.status}")
                if not job.terminal:
                    print("cooperative: a line in flight finishes; ffmpeg is not killed")
                return EXIT_OK
            job = store.retry(args.job_id, max_retries=settings.max_retries)
        except JobError as exc:
            print(f"dubforge: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"{job.id}: {job.status} (attempt {job.attempts})")
        print("run `dubforge jobs show " + job.id + "` to follow it")
        return EXIT_OK

    if action == "cleanup":
        result = store.cleanup(
            older_than_days=args.older_than or None,
            dry_run=not args.yes,
        )
        if args.json:
            _emit_json(result)
            return EXIT_OK
        verb = "would remove" if result["dry_run"] else "removed"
        print(f"{verb} {len(result['removed'])} job(s), kept {result['kept']}")
        for job_id in result["removed"][:20]:
            print(f"  {job_id}")
        if result["dry_run"] and result["removed"]:
            print("re-run with --yes to delete")
        return EXIT_OK

    print(f"dubforge: unknown jobs action {action!r}", file=sys.stderr)
    return EXIT_ERROR


def cmd_batch(args) -> int:
    settings = _settings(args)
    try:
        sources = batch.collect_sources(
            args.target, recursive=not args.no_recursive, store=JobStore(settings.workdir)
        )
    except FileNotFoundError as exc:
        print(f"dubforge: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not sources:
        print(f"dubforge: no media files in {args.target}", file=sys.stderr)
        return EXIT_ERROR
    result = batch.run_batch(
        sources,
        settings,
        force=args.force,
        stop_on_error=args.stop_on_error,
        estimate=args.estimate,
        log=None if args.json else print,
    )
    if args.json:
        _emit_json(result.to_dict())
    else:
        print()
        print(result.table())
        if args.estimate and result.estimates.get("total_usd") is not None:
            print(
                f"estimated total: ${result.estimates['total_usd']:.2f} (list prices, not a quote)"
            )
    return EXIT_OK if result.ok else EXIT_QC_FAILED


def cmd_regen(args) -> int:
    """Re-dub one job with edited lines, reusing every untouched render."""
    settings = _settings(args)
    edits: dict[str, str] = {}
    for item in args.set or []:
        if "=" not in item:
            print(f"dubforge: --set needs SEGMENT=TEXT, got {item!r}", file=sys.stderr)
            return EXIT_ERROR
        key, text = item.split("=", 1)
        edits[key.strip()] = text
    if not edits and not args.output:
        print("dubforge: nothing to do — pass --set SEGMENT=TEXT", file=sys.stderr)
        return EXIT_ERROR
    try:
        result = pipeline.regenerate(
            args.job_id,
            settings,
            edits=edits,
            output=args.out or None,
            log=None if args.json else print,
        )
    except (pipeline.DubError, ProviderError, ProviderUnavailable) as exc:
        print(f"dubforge: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        _emit_json(result.to_dict())
    else:
        print()
        print(qc.summary(result.qc))
        print(f"\n{result.rendered_segments} line(s) rendered, {result.reused_segments} reused")
        print(f"output: {result.output}")
    return EXIT_OK if result.ok else EXIT_QC_FAILED


def cmd_mcp(args) -> int:
    from . import server

    server.main()
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dubforge",
        description="Dub any video into any language with several AI providers in one pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"dubforge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="JSON settings file (keys and values as in Settings)")
        p.add_argument("--workdir", help="where intermediates go (default dubforge-work)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("doctor", help="what this machine can and cannot do")
    common(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("providers", help="every adapter, its readiness and its honest quality note")
    common(p)
    p.add_argument("--kind", choices=registry.KINDS)
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("voices", help="list voices for a TTS provider")
    common(p)
    p.add_argument("--tts", help="provider name (default: auto)")
    p.add_argument("--lang", help="language code, e.g. hi")
    p.set_defaults(func=cmd_voices)

    p = sub.add_parser(
        "plan", help="probe the file, resolve providers, estimate cost — no work done"
    )
    common(p)
    p.add_argument("input", help="video or audio file")
    _language_args(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("dub", help="dub the file")
    common(p)
    p.add_argument("input", help="video or audio file")
    p.add_argument("--out", help="output file (default: <name>.<lang>.mp4 next to the input)")
    _language_args(p)
    _quality_args(p)
    p.set_defaults(func=cmd_dub)

    p = sub.add_parser("demo", help="run the whole pipeline offline on a synthetic clip")
    common(p)
    p.add_argument("--out-dir", default="dubforge-demo")
    p.add_argument("--to", default="hi", help="target language label for the demo (default hi)")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("srt", help="write subtitles from a transcript dump")
    common(p)
    p.add_argument("input", help="transcript .json written by a previous run")
    p.add_argument("--out-dir", default=".")
    p.add_argument("--stem", default="dub")
    p.set_defaults(func=cmd_srt)

    p = sub.add_parser("capabilities", help="what this install can do, in JSON or prose")
    common(p)
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("validate-config", help="check a settings file and the environment")
    common(p)
    p.set_defaults(func=cmd_validate_config)

    p = sub.add_parser("jobs", help="list, inspect, cancel, retry or clean up jobs")
    common(p)
    p.add_argument("action", choices=["list", "show", "cancel", "retry", "cleanup"])
    p.add_argument("job_id", nargs="?", help="job id, for show/cancel/retry")
    p.add_argument(
        "--status", help="filter: queued, processing, completed, failed, cancelled, interrupted"
    )
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--older-than", type=int, default=0, help="cleanup: only jobs older than N days")
    p.add_argument(
        "--yes", action="store_true", help="cleanup: actually delete (default is a dry run)"
    )
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("regen", help="re-dub one job with edited lines, reusing untouched renders")
    common(p)
    p.add_argument("job_id", help="the job to regenerate")
    p.add_argument(
        "--set",
        action="append",
        metavar="SEG=TEXT",
        help="replace a line's translation, by segment id or index (repeatable)",
    )
    p.add_argument("--out", help="output path (default: <previous>.r<N>.mp4, never the old file)")
    p.set_defaults(func=cmd_regen)

    p = sub.add_parser("batch", help="dub a folder (or a list of files) through the same pipeline")
    common(p)
    p.add_argument("target", help="folder of media files, or a single file")
    _language_args(p)
    _quality_args(p)
    p.add_argument("--force", action="store_true", help="re-dub files that already passed QC")
    p.add_argument(
        "--stop-on-error", action="store_true", help="stop the batch on the first failure"
    )
    p.add_argument("--estimate", action="store_true", help="also estimate the cost per file")
    p.add_argument("--no-recursive", action="store_true", help="do not descend into sub-folders")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("mcp", help="run the MCP server on stdio")
    p.set_defaults(func=cmd_mcp)

    return parser


def _language_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="source_lang", help="source language code, or auto")
    p.add_argument("--to", dest="target_lang", help="target language code, e.g. hi, ta, es, en")
    p.add_argument("--asr", help="speech recognition provider (default auto)")
    p.add_argument("--translator", help="translation provider (default auto)")
    p.add_argument("--tts", help="text-to-speech provider (default auto)")
    p.add_argument("--transcript", help="reuse a .json transcript or .srt instead of running ASR")


def _quality_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--voice-pool", help="comma-separated voice ids to assign to speakers in order")
    p.add_argument("--voice-map", help="SPEAKER_00=voiceid,SPEAKER_01=voiceid2")
    p.add_argument("--glossary", help='terms to pin, e.g. "ISRO=इसरो,UPI=यूपीआई"')
    p.add_argument("--style", help="delivery brief for the voice and the translator")
    p.add_argument("--emotion", help="emotion hint where the provider supports it")
    p.add_argument("--context", help="what the video is about; improves terminology")
    p.add_argument(
        "--max-speed", type=float, help="fastest atempo before asking for a rewrite (default 1.15)"
    )
    p.add_argument(
        "--rewrite-rounds", type=int, help="how many times to ask for shorter lines (default 2)"
    )
    p.add_argument("--separator", help="stem separator (demucs | ffmpeg_center | none)")
    p.add_argument(
        "--no-background",
        action="store_true",
        help="replace the whole soundtrack instead of keeping it",
    )
    p.add_argument(
        "--bed-gain-db", type=float, help="level of the kept music/effects bed (default -16)"
    )
    p.add_argument("--voice-gain-db", type=float, help="level of the dub (default 0)")
    p.add_argument("--no-duck", action="store_true", help="do not duck the bed under the dialogue")
    p.add_argument("--loudness", dest="loudness_lufs", type=float, help="target LUFS (default -16)")
    p.add_argument("--lipsync", help="lip sync provider (none by default)")
    p.add_argument("--asr-model", help="override the ASR model")
    p.add_argument("--translate-model", help="override the translation model")
    p.add_argument("--tts-model", help="override the TTS model")
    p.add_argument("--no-cache", action="store_true", help="ignore a cached transcript")
    p.add_argument("--dry-run", action="store_true", help="plan only, render nothing")


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"dubforge: configuration error\n{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ProviderUnavailable as exc:
        print(f"dubforge: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (pipeline.DubError, ffmpeg.FFmpegError, ffmpeg.FFmpegMissing, ProviderError) as exc:
        print(f"dubforge: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("dubforge: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
