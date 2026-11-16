"""`dubforge doctor` — never guess why a stage is unavailable.

Reports the OS, the ffmpeg that will actually be used (and where it came from),
whether the filters this pipeline depends on exist in that build, which optional
packages are installed, and — per stage — what `auto` would pick and what is
missing. Environment variables are reported as set or unset; values are never
printed.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import ffmpeg
from .config import Settings
from .providers import registry

REQUIRED_FILTERS = (
    "atempo",
    "adelay",
    "amix",
    "apad",
    "atrim",
    "loudnorm",
    "aresample",
    "sidechaincompress",
    "silencedetect",
    "volumedetect",
    "aformat",
)


def _has_filters(binary: str) -> dict:
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {"checked": False, "missing": list(REQUIRED_FILTERS)}
    missing = [f for f in REQUIRED_FILTERS if f" {f} " not in out]
    return {"checked": True, "missing": missing}


def _has_encoders(binary: str) -> dict:
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {"aac": False, "h264": False}
    return {"aac": " aac " in out, "h264": ("libx264" in out) or (" h264 " in out)}


def checks(settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    report: dict = {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "ok": True,
        "hints": [],
    }

    try:
        binary = ffmpeg.ffmpeg_path()
        report["ffmpeg"] = {
            "path": binary,
            "source": ffmpeg.ffmpeg_source(),
            "version": ffmpeg.version(),
            "filters": _has_filters(binary),
            "encoders": _has_encoders(binary),
        }
        if report["ffmpeg"]["filters"]["missing"]:
            report["ok"] = False
            report["hints"].append(
                "this ffmpeg build is missing filters the pipeline needs: "
                + ", ".join(report["ffmpeg"]["filters"]["missing"])
                + " — install a full build (apt install ffmpeg / brew install ffmpeg / Gyan.FFmpeg on Windows)"
            )
        if not report["ffmpeg"]["encoders"].get("aac"):
            report["hints"].append("no aac encoder in this ffmpeg: the final mux will fail")
    except ffmpeg.FFmpegMissing as exc:
        report["ok"] = False
        report["ffmpeg"] = {"path": None, "source": "missing"}
        report["hints"].append(str(exc))

    work = Path(settings.workdir)
    try:
        work.mkdir(parents=True, exist_ok=True)
        probe = work / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["workdir"] = {"path": str(work.resolve()), "writable": True}
    except OSError as exc:
        report["workdir"] = {"path": str(work), "writable": False, "error": str(exc)}
        report["ok"] = False
        report["hints"].append(f"workdir is not writable: {exc} — set --workdir somewhere you own")

    report["packages"] = {
        "requests": _importable("requests"),
        "imageio_ffmpeg": _importable("imageio_ffmpeg"),
        "fastmcp": _importable("fastmcp"),
        "faster_whisper": _importable("faster_whisper"),
        "demucs": _importable("demucs"),
        "edge_tts": _importable("edge_tts"),
        "pyannote.audio": _importable("pyannote.audio"),
    }
    report["binaries"] = {name: bool(shutil.which(name)) for name in ("ffmpeg", "demucs", "ollama")}
    report["env"] = {
        var: bool(os.environ.get(var))
        for var in sorted(
            {
                "OPENAI_API_KEY",
                "DEEPGRAM_API_KEY",
                "ELEVENLABS_API_KEY",
                "GROQ_API_KEY",
                "SARVAM_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "DEEPL_API_KEY",
                "SYNCLABS_API_KEY",
                "DUBFORGE_FFMPEG",
            }
        )
    }
    report["providers"] = registry.ready_summary(settings)
    report["ready"] = {k: v.get("ready") for k, v in report["providers"].items()}

    for kind, row in report["providers"].items():
        if not row["ready"] and kind not in ("lipsync",):
            report["hints"].append(
                f"{kind}: nothing ready — {row['missing'][0] if row['missing'] else 'see providers'}"
            )

    report["settings"] = {
        "target_lang": settings.target_lang,
        "source_lang": settings.source_lang,
        "max_speed": settings.max_speed,
        "keep_background": settings.keep_background,
        "loudness_lufs": settings.loudness_lufs,
    }
    return report


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def text(settings: Settings | None = None) -> str:
    r = checks(settings)
    lines = [
        f"dubforge doctor — {r['os']}, Python {r['python']}",
        "",
    ]
    ff = r.get("ffmpeg") or {}
    lines.append(f"ffmpeg        {ff.get('version') or 'MISSING'}")
    lines.append(f"              from {ff.get('source')} at {ff.get('path')}")
    if ff.get("filters", {}).get("missing"):
        lines.append(f"              MISSING filters: {', '.join(ff['filters']['missing'])}")
    else:
        lines.append("              all required filters present")
    enc = ff.get("encoders") or {}
    lines.append(
        f"              encoders: aac={'yes' if enc.get('aac') else 'no'}, h264={'yes' if enc.get('h264') else 'no'}"
    )
    lines.append(f"workdir       {r['workdir']['path']} (writable={r['workdir']['writable']})")
    lines.append("")
    lines.append("packages")
    for name, present in r["packages"].items():
        lines.append(f"  {'ok ' if present else '-- '} {name}")
    lines.append("")
    lines.append("keys (set / not set — values are never shown)")
    for var, present in r["env"].items():
        lines.append(f"  {'set' if present else 'not set'}  {var}")
    lines.append("")
    lines.append("stages — what 'auto' would pick right now")
    for kind, row in r["providers"].items():
        if row["ready"]:
            lines.append(f"  {kind:<10} {row['auto']}")
        else:
            first = row["missing"][0] if row["missing"] else "nothing available"
            lines.append(f"  {kind:<10} NOT READY — {first}")
    if r["hints"]:
        lines.append("")
        lines.append("hints")
        lines += [f"  - {h}" for h in r["hints"]]
    lines.append("")
    lines.append("Run `dubforge providers` for every adapter, including the unverified ones.")
    return "\n".join(lines)


def to_json(settings: Settings | None = None) -> str:
    return json.dumps(checks(settings), ensure_ascii=False, indent=2)
