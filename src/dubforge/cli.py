"""`dubforge` on the command line.

Everything the MCP server can do is here too, because the first thing anyone does
with a new tool is try to run it by hand, and the second is put it in a cron job.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, demo, doctor, ffmpeg, pipeline, qc, srt
from .config import Settings
from .media import Transcript
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable

EXIT_OK, EXIT_QC_FAILED, EXIT_ERROR = 0, 1, 2


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
    print(doctor.to_json(settings) if args.json else doctor.text(settings))
    return EXIT_OK


def cmd_providers(args) -> int:
    settings = _settings(args)
    rows = registry.all_specs(settings)
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
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
        print(json.dumps([v.to_dict() for v in voices], ensure_ascii=False, indent=2))
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
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
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
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
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
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
    print(json.dumps(out, indent=2))
    return EXIT_OK


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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
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
