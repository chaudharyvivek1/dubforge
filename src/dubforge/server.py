"""MCP server: the pipeline as agent tools.

Two rules shape this file.

**Never hand an agent a wall of data.** Tools return a short summary plus a path. A
40-minute transcript is a file, not a tool result.

**Never hand an agent a stack trace.** Every tool validates its arguments, and every
failure comes back as the same shape::

    {"ok": false, "error": {"code": "missing_file", "message": "...", "hint": "..."}}

`code` is stable and machine-readable (`invalid_argument`, `missing_file`,
`unsupported_media`, `no_provider`, `provider_error`, `io_error`, `internal_error`), so
an agent can decide what to do without parsing prose. No message ever contains a key:
`dubforge doctor` reports whether a variable is set, never its value.

Tool names are part of the public surface and do not change.

    pip install "dubforge[mcp]"
    dubforge-mcp                     # stdio
    python -m dubforge.server        # same thing
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from . import __version__, doctor, ffmpeg, pipeline, qc, srt
from .config import Settings, is_language_code
from .media import Transcript
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable
from .workspace import open_job

MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".avi",
    ".mpeg",
    ".mpg",
    ".ts",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
}

ERROR_CODES = (
    "invalid_argument",
    "missing_file",
    "unsupported_media",
    "no_provider",
    "provider_error",
    "io_error",
    "internal_error",
)


# --------------------------------------------------------------------------- errors


class ToolError(Exception):
    """An argument or environment problem, phrased for an agent to act on."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2)


def _err(code: str, message: str, hint: str = "", **extra: Any) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message, "hint": hint, **extra}},
        ensure_ascii=False,
        indent=2,
    )


def guard(fn):
    """Turn every exception into a structured, secret-free error payload."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return _err(exc.code, exc.message, exc.hint)
        except ProviderUnavailable as exc:
            return _err(
                "no_provider",
                str(exc).splitlines()[0],
                "Run dubforge_doctor, then set the missing key or install the package.",
            )
        except ProviderError as exc:
            return _err(
                "provider_error", str(exc)[:600], "Check the provider key, model and quota."
            )
        except pipeline.DubError as exc:
            return _err("invalid_argument", str(exc)[:600], "Adjust the request and retry.")
        except ffmpeg.FFmpegMissing as exc:
            return _err("io_error", str(exc)[:600], "Install ffmpeg, then run dubforge_doctor.")
        except ffmpeg.FFmpegError as exc:
            return _err(
                "unsupported_media", str(exc)[:600], "Probe the file first with probe_media."
            )
        except (PermissionError, OSError) as exc:
            return _err(
                "io_error", f"{type(exc).__name__}: {exc}"[:600], "Check paths and permissions."
            )
        except Exception as exc:  # noqa: BLE001 - an agent must never receive a traceback
            return _err(
                "internal_error",
                f"{type(exc).__name__}: {exc}"[:600],
                "This is a bug. Re-run with the same arguments and report it with qc.json.",
            )

    return wrapper


# ----------------------------------------------------------------------- validation


def _path(value: str, field: str = "path", must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("invalid_argument", f"{field} must be a non-empty path string")
    candidate = Path(value).expanduser()
    if must_exist and not candidate.exists():
        raise ToolError(
            "missing_file",
            f"{field} does not exist: {value}",
            "Pass an absolute path to a real file.",
        )
    if candidate.exists() and not candidate.is_file() and not candidate.is_dir():
        raise ToolError("invalid_argument", f"{field} is not a regular file: {value}")
    return candidate


def _media_path(value: str) -> Path:
    candidate = _path(value, "path")
    if not candidate.is_file():
        raise ToolError("invalid_argument", f"path is a directory, not a media file: {value}")
    if candidate.suffix.lower() not in MEDIA_SUFFIXES:
        # Not fatal — ffmpeg decides what it can read — but worth saying out loud.
        pass
    return candidate


def _lang(value: str, field: str, allow_auto: bool = False) -> str:
    code = (value or "").strip()
    if allow_auto and code.lower() == "auto":
        return "auto"
    if not is_language_code(code):
        raise ToolError(
            "invalid_argument",
            f"{field} must be a language code like 'hi', 'ta' or 'pt-BR'; got {value!r}",
            "Use the ISO 639-1 code: hi, bn, ta, te, mr, gu, kn, ml, pa, or, as, ur, en, es, fr, de…",
        )
    return code


def _number(value: Any, field: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_argument", f"{field} must be a number; got {value!r}") from exc
    if not low <= number <= high:
        raise ToolError("invalid_argument", f"{field}={number} is outside {low}–{high}")
    return number


def _pairs(raw: str, field: str, example: str) -> dict[str, str]:
    """`A=B,C=D` → dict, with a message that says what was wrong."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in _split_top_level(raw):
        if "=" not in chunk:
            raise ToolError(
                "invalid_argument",
                f"{field} entry {chunk!r} has no '='",
                f"Format: {example}",
            )
        key, value = chunk.split("=", 1)
        key, value = key.strip().strip('"'), value.strip().strip('"')
        if not key or not value:
            raise ToolError(
                "invalid_argument",
                f"{field} entry {chunk!r} is missing a key or a value",
                f"Format: {example}",
            )
        out[key] = value
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
    return [part.strip() for part in parts if part.strip()]


def _known(kind: str, name: str, field: str) -> str:
    """Validate a provider name without requiring it to be configured."""
    if not name:
        return ""
    available = registry.names(kind)
    if name not in available:
        raise ToolError(
            "invalid_argument",
            f"unknown {field} provider {name!r}",
            f"Available: {', '.join(sorted(available))}",
        )
    return name


def _settings(**kwargs) -> Settings:
    clean = {key: value for key, value in kwargs.items() if value not in (None, "")}
    return Settings.from_env(**clean)


def build_server():
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise SystemExit('the MCP server needs fastmcp: pip install "dubforge[mcp]"') from exc

    mcp = FastMCP(
        name="dubforge",
        instructions=(
            "Dub video and audio into another language with a quality-controlled pipeline.\n"
            "Order of operations that works:\n"
            "1. `dubforge_doctor` — never guess whether a stage is available.\n"
            "2. `plan_dub` — get the cost, the provider choice and any missing keys BEFORE spending money.\n"
            "3. `dub_video` — run it. Read `qc`; if `ok` is false, read `failures` and fix the cause.\n"
            "4. Report the QC summary to the user and point at the files. Never claim a dub is good "
            "because it finished: a run can finish and still fail QC.\n\n"
            'Every tool returns {"ok": true, ...} or '
            '{"ok": false, "error": {"code", "message", "hint"}}. Codes are stable: '
            "invalid_argument, missing_file, unsupported_media, no_provider, provider_error, "
            "io_error, internal_error.\n\n"
            "Never paste a transcript into the conversation. It is a file; pass the path."
        ),
    )

    # ------------------------------------------------------------------ doctor
    @mcp.tool()
    @guard
    def dubforge_doctor() -> str:
        """Report ffmpeg, optional packages, API keys (set/not set) and which provider each stage would use."""
        report = doctor.checks()
        return _ok(
            ffmpeg=report["ffmpeg"].get("version"),
            ffmpeg_source=report["ffmpeg"].get("source"),
            missing_filters=report["ffmpeg"].get("filters", {}).get("missing", []),
            workdir=report["workdir"],
            packages=report["packages"],
            env=report["env"],
            stages=report["ready"],
            hints=report["hints"],
            usable=report["ok"],
        )

    # --------------------------------------------------------------- providers
    @mcp.tool()
    @guard
    def list_providers(kind: str = "") -> str:
        """Every adapter with readiness, capabilities, honest quality note and the `verified` flag.

        kind: asr | translate | tts | separator | lipsync, or "" for all.
        """
        if kind and kind not in registry.KINDS:
            raise ToolError(
                "invalid_argument",
                f"unknown stage {kind!r}",
                f"Use one of: {', '.join(registry.KINDS)}",
            )
        rows = registry.all_specs()
        if kind:
            rows = [row for row in rows if row["kind"] == kind]
        return _ok(
            providers=[
                {
                    "kind": row["kind"],
                    "name": row["name"],
                    "title": row["title"],
                    "ready": row["available"],
                    "missing": row["missing"],
                    "verified": row["verified"],
                    "capabilities": row["capabilities"],
                    "languages": row["languages"],
                    "quality": row["quality"],
                    "auto_rank": row["auto_rank"],
                }
                for row in rows
            ]
        )

    # ------------------------------------------------------------------- media
    @mcp.tool()
    @guard
    def probe_media(path: str) -> str:
        """Duration, resolution, fps and audio layout of a media file. Start here for an unknown file."""
        candidate = _media_path(path)
        info = ffmpeg.probe(candidate)
        return _ok(media=info.to_dict(), summary=info.summary)

    # -------------------------------------------------------------------- plan
    @mcp.tool()
    @guard
    def plan_dub(
        path: str,
        target_lang: str,
        source_lang: str = "auto",
        asr: str = "",
        translator: str = "",
        tts: str = "",
        separator: str = "",
        keep_background: bool = True,
        max_speed: float = 1.15,
        style: str = "",
        context: str = "",
    ) -> str:
        """Cost, provider choice, timing budget and blockers for a dub. Call this before dub_video."""
        candidate = _media_path(path)
        target = _lang(target_lang, "target_lang")
        source = _lang(source_lang or "auto", "source_lang", allow_auto=True)
        settings = _settings(
            target_lang=target,
            source_lang=source,
            asr=_known("asr", asr, "asr") or None,
            translator=_known("translate", translator, "translator") or None,
            tts=_known("tts", tts, "tts") or None,
            separator=_known("separator", separator, "separator") or None,
            keep_background=bool(keep_background),
            max_speed=_number(max_speed, "max_speed", 0.5, 2.0),
            style=style or None,
            context=context or None,
        )
        result = pipeline.plan(candidate, settings)
        data = result.to_dict()
        return _ok(ready=result.ready, plan=data, summary=result.text())

    # ------------------------------------------------------------- transcribe
    @mcp.tool()
    @guard
    def transcribe_video(
        path: str, language: str = "auto", asr: str = "", diarize: bool = True
    ) -> str:
        """Transcribe with timings and speakers. Writes a transcript JSON and returns its path plus a preview."""
        candidate = _media_path(path)
        source = _lang(language or "auto", "language", allow_auto=True)
        settings = _settings(source_lang=source, asr=_known("asr", asr, "asr") or None)
        provider = registry.resolve("asr", settings)
        job = open_job(settings.workdir, candidate, cache_key=f"asr-{provider.name}")
        try:
            audio = ffmpeg.extract_audio(candidate, job.dir("asr") / "input.wav", 16000, 1)
            transcript = provider.transcribe(audio, source, diarize=bool(diarize))
        finally:
            job.release()
        if not transcript.segments:
            raise ToolError(
                "unsupported_media",
                "no speech was found in the audio",
                "Check the file with probe_media; for a music-only file there is nothing to dub.",
            )
        out = transcript.save(job.path / "transcript.json")
        return _ok(
            provider=provider.name,
            language=transcript.language,
            segments=len(transcript.segments),
            speakers=transcript.speakers,
            transcript_path=out,
            preview=[
                {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text[:120]}
                for s in transcript.segments[:5]
            ],
            note="full transcript is in the file; do not paste it into the conversation",
        )

    # --------------------------------------------------------------------- dub
    @mcp.tool()
    @guard
    def dub_video(
        path: str,
        target_lang: str,
        source_lang: str = "auto",
        output: str = "",
        asr: str = "",
        translator: str = "",
        tts: str = "",
        separator: str = "",
        voice_pool: str = "",
        voice_map: str = "",
        glossary: str = "",
        style: str = "",
        emotion: str = "",
        context: str = "",
        keep_background: bool = True,
        max_speed: float = 1.15,
        rewrite_rounds: int = 2,
        loudness_lufs: float = -16.0,
        dry_run: bool = False,
        transcript: str = "",
    ) -> str:
        """Dub a video or audio file. Returns output paths, a QC verdict and any warnings.

        voice_pool: comma-separated voice ids, assigned to speakers in order.
        voice_map:  SPEAKER_00=id,SPEAKER_01=id
        glossary:   term=rendering,term2=rendering2
        """
        candidate = _media_path(path)
        target = _lang(target_lang, "target_lang")
        source = _lang(source_lang or "auto", "source_lang", allow_auto=True)
        if output:
            _path(output, "output", must_exist=False)
            if Path(output).expanduser().resolve() == candidate.resolve():
                raise ToolError(
                    "invalid_argument",
                    "output would overwrite the source file",
                    "Pass a different output path, or leave it empty and dubforge will name the file.",
                )
        if transcript:
            _path(transcript, "transcript")
        settings = _settings(
            target_lang=target,
            source_lang=source,
            asr=_known("asr", asr, "asr") or None,
            translator=_known("translate", translator, "translator") or None,
            tts=_known("tts", tts, "tts") or None,
            separator=_known("separator", separator, "separator") or None,
            voice_pool=[v.strip() for v in voice_pool.split(",") if v.strip()]
            if voice_pool
            else None,
            voice_map=_pairs(voice_map, "voice_map", "SPEAKER_00=<voice id>,SPEAKER_01=<voice id>")
            or None,
            glossary=_pairs(glossary, "glossary", "ISRO=इसरो,UPI=यूपीआई") or None,
            style=style or None,
            emotion=emotion or None,
            context=context or None,
            keep_background=bool(keep_background),
            max_speed=_number(max_speed, "max_speed", 0.5, 2.0),
            rewrite_rounds=int(_number(rewrite_rounds, "rewrite_rounds", 0, 5)),
            loudness_lufs=_number(loudness_lufs, "loudness_lufs", -40.0, -5.0),
            dry_run=bool(dry_run),
            transcript=transcript or None,
        )
        invalid = settings.validate()
        if invalid:
            raise ToolError(
                "invalid_argument", "; ".join(invalid), "Fix the named settings and retry."
            )
        result = pipeline.dub(candidate, settings, output=output or None)
        data = result.to_dict()
        return _ok(
            qc_summary=qc.summary(result.qc),
            output=result.output,
            audio=result.audio,
            workdir=result.workdir,
            transcript_json=result.transcript_json,
            subtitles=result.subtitles,
            report=result.report,
            qc=result.qc,
            steps=data["steps"],
            warnings=result.warnings,
        )

    # ---------------------------------------------------------------------- QC
    @mcp.tool()
    @guard
    def get_qc(workdir: str) -> str:
        """Read the QC report of a finished run from its work directory."""
        directory = _path(workdir, "workdir")
        if not directory.is_dir():
            raise ToolError("invalid_argument", f"workdir is not a directory: {workdir}")
        path = directory / "qc.json"
        if not path.exists():
            raise ToolError(
                "missing_file",
                f"no qc.json in {workdir}",
                "Pass the `workdir` value that dub_video returned, not the output file's folder.",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ToolError("io_error", f"qc.json is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise ToolError("io_error", "qc.json does not contain a QC object")
        return _ok(qc=data, summary=qc.summary(data))

    # ------------------------------------------------------------------ voices
    @mcp.tool()
    @guard
    def list_voices(provider: str = "", language: str = "hi") -> str:
        """Voices a TTS provider offers for a language. Empty provider means 'whatever auto picks'."""
        lang = _lang(language, "language")
        name = _known("tts", provider, "tts")
        settings = _settings(target_lang=lang, tts=name or None)
        tts_provider = registry.resolve("tts", settings, name or None)
        voices = tts_provider.voices(lang)
        return _ok(
            provider=tts_provider.name,
            count=len(voices),
            voices=[voice.to_dict() for voice in voices[:60]],
            note=""
            if voices
            else "this provider returned no list; it may need a key, a package or a different language",
        )

    # ------------------------------------------------------------- subtitles
    @mcp.tool()
    @guard
    def write_subtitles(transcript_json: str, out_dir: str = "") -> str:
        """Turn a transcript dump into source / target / bilingual SRT and a WebVTT file."""
        path = _path(transcript_json, "transcript_json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ToolError(
                "invalid_argument",
                f"{transcript_json} is not readable JSON: {exc}",
                "Pass a transcript.dub.json written by dub_video or transcribe_video.",
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
            raise ToolError(
                "invalid_argument",
                f"{transcript_json} is not a dubforge transcript (no 'segments' list)",
                "Pass a transcript.dub.json written by dub_video or transcribe_video.",
            )
        try:
            transcript = Transcript.from_dict(raw)
        except (TypeError, KeyError, ValueError) as exc:
            raise ToolError("invalid_argument", f"transcript is malformed: {exc}") from exc
        if not transcript.segments:
            raise ToolError("invalid_argument", "transcript has no segments to write")
        target_dir = _path(out_dir, "out_dir", must_exist=False) if out_dir else path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        return _ok(**srt.write_all(transcript.segments, target_dir, path.stem.replace(".dub", "")))

    @mcp.tool()
    @guard
    def dubforge_version() -> str:
        """Version of the running dubforge package, plus the error codes this server can return."""
        return _ok(version=__version__, error_codes=list(ERROR_CODES))

    return mcp


def main() -> None:
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
