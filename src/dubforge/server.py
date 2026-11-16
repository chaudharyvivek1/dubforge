"""MCP server: the pipeline as agent tools.

The rule that shapes this file is the same one that shapes a good dubbing
assistant: never hand an agent a wall of data. Tools return a short summary plus a
path. A 40-minute transcript is a file, not a tool result.

    pip install "dubforge[mcp]"
    dubforge-mcp                     # stdio
    python -m dubforge.server        # same thing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__, doctor, ffmpeg, pipeline, qc, srt
from .config import Settings
from .media import Transcript
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _settings(**kwargs) -> Settings:
    clean = {k: v for k, v in kwargs.items() if v not in (None, "")}
    if "voice_pool" in clean and isinstance(clean["voice_pool"], str):
        clean["voice_pool"] = [v.strip() for v in clean["voice_pool"].split(",") if v.strip()]
    if "glossary" in clean and isinstance(clean["glossary"], str):
        clean["glossary"] = {
            k.strip(): v.strip()
            for k, v in (pair.split("=", 1) for pair in clean["glossary"].split(",") if "=" in pair)
        }
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
            "because it finished: a run can finish and still fail QC.\n"
            "Never paste a transcript into the conversation. It is a file; pass the path."
        ),
    )

    @mcp.tool()
    def dubforge_doctor() -> str:
        """Report ffmpeg, optional packages, API keys (set/not set) and which provider each stage would use."""
        report = doctor.checks()
        return _dumps(
            {
                "ok": report["ok"],
                "ffmpeg": report["ffmpeg"].get("version"),
                "ffmpeg_source": report["ffmpeg"].get("source"),
                "missing_filters": report["ffmpeg"].get("filters", {}).get("missing", []),
                "workdir": report["workdir"],
                "packages": report["packages"],
                "env": report["env"],
                "stages": report["ready"],
                "hints": report["hints"],
            }
        )

    @mcp.tool()
    def list_providers(kind: str = "") -> str:
        """Every adapter with readiness, capabilities, honest quality note and the `verified` flag.

        kind: asr | translate | tts | separator | lipsync, or "" for all.
        """
        rows = registry.all_specs()
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        slim = [
            {
                "kind": r["kind"],
                "name": r["name"],
                "title": r["title"],
                "ready": r["available"],
                "missing": r["missing"],
                "verified": r["verified"],
                "capabilities": r["capabilities"],
                "languages": r["languages"],
                "quality": r["quality"],
                "auto_rank": r["auto_rank"],
            }
            for r in rows
        ]
        return _dumps(slim)

    @mcp.tool()
    def probe_media(path: str) -> str:
        """Duration, resolution, fps and audio layout of a media file. Start here for an unknown file."""
        try:
            return _dumps(ffmpeg.probe(path).to_dict())
        except ffmpeg.FFmpegError as exc:
            return _dumps({"ok": False, "error": str(exc)})

    @mcp.tool()
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
        settings = _settings(
            target_lang=target_lang,
            source_lang=source_lang,
            asr=asr or None,
            translator=translator or None,
            tts=tts or None,
            separator=separator or None,
            keep_background=keep_background,
            max_speed=max_speed,
            style=style or None,
            context=context or None,
        )
        try:
            result = pipeline.plan(path, settings)
        except (pipeline.DubError, ffmpeg.FFmpegError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        data = result.to_dict()
        data["ok"] = result.ready
        data["summary"] = result.text()
        return _dumps(data)

    @mcp.tool()
    def transcribe_video(
        path: str, language: str = "auto", asr: str = "", diarize: bool = True
    ) -> str:
        """Transcribe with timings and speakers. Writes a transcript JSON and returns its path plus a preview."""
        settings = _settings(language=None, source_lang=language, asr=asr or None)
        try:
            provider = registry.resolve("asr", settings)
            audio = ffmpeg.extract_audio(
                path, str(Path(settings.workdir) / "asr" / "input.wav"), 16000, 1
            )
            transcript = provider.transcribe(audio, language, diarize=diarize)
        except (ProviderError, ffmpeg.FFmpegError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        out = transcript.save(Path(settings.workdir) / "asr" / "transcript.json")
        return _dumps(
            {
                "ok": True,
                "provider": provider.name,
                "language": transcript.language,
                "segments": len(transcript.segments),
                "speakers": transcript.speakers,
                "transcript_path": out,
                "preview": [
                    {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text[:120]}
                    for s in transcript.segments[:5]
                ],
                "note": "full transcript is in the file; do not paste it into the conversation",
            }
        )

    @mcp.tool()
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
        settings = _settings(
            target_lang=target_lang,
            source_lang=source_lang,
            asr=asr or None,
            translator=translator or None,
            tts=tts or None,
            separator=separator or None,
            voice_pool=voice_pool or None,
            voice_map=voice_map or None,
            glossary=glossary or None,
            style=style or None,
            emotion=emotion or None,
            context=context or None,
            keep_background=keep_background,
            max_speed=max_speed,
            rewrite_rounds=rewrite_rounds,
            loudness_lufs=loudness_lufs,
            dry_run=dry_run,
            transcript=transcript or None,
        )
        try:
            result = pipeline.dub(path, settings, output=output or None)
        except (pipeline.DubError, ProviderError, ProviderUnavailable, ffmpeg.FFmpegError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        data = result.to_dict()
        data["qc_summary"] = qc.summary(result.qc)
        return _dumps(data)

    @mcp.tool()
    def get_qc(workdir: str) -> str:
        """Read the QC report of a finished run from its work directory."""
        path = Path(workdir) / "qc.json"
        if not path.exists():
            return _dumps({"ok": False, "error": f"no qc.json in {workdir}"})
        data = json.loads(path.read_text(encoding="utf-8"))
        data["summary"] = qc.summary(data)
        return _dumps(data)

    @mcp.tool()
    def list_voices(provider: str = "", language: str = "hi") -> str:
        """Voices a TTS provider offers for a language. Empty provider means 'whatever auto picks'."""
        settings = _settings(target_lang=language, tts=provider or None)
        try:
            tts = registry.resolve("tts", settings, provider or None)
            voices = tts.voices(language)
        except ProviderError as exc:
            return _dumps({"ok": False, "error": str(exc)})
        return _dumps(
            {
                "ok": True,
                "provider": tts.name,
                "count": len(voices),
                "voices": [v.to_dict() for v in voices[:60]],
                "note": ""
                if voices
                else "this provider returned no list; it may need a key or a package",
            }
        )

    @mcp.tool()
    def write_subtitles(transcript_json: str, out_dir: str = "") -> str:
        """Turn a transcript dump into source / target / bilingual SRT and a WebVTT file."""
        try:
            transcript = Transcript.load(transcript_json)
        except (OSError, json.JSONDecodeError) as exc:
            return _dumps({"ok": False, "error": str(exc)})
        target_dir = out_dir or str(Path(transcript_json).parent)
        return _dumps({"ok": True, **srt.write_all(transcript.segments, target_dir, "dub")})

    @mcp.tool()
    def dubforge_version() -> str:
        """Version of the running dubforge package."""
        return __version__

    return mcp


def main() -> None:
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
