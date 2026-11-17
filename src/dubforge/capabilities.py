"""Capability discovery: one machine-readable answer to "what can this install do?".

Everything here is *derived*, never hardcoded: the provider lists come from the registry
and each adapter's own `Spec`, readiness from `missing()`, the feature flags from what
those specs actually declare, and the limits from `Settings`. `doctor`, `providers`,
`plan` and the MCP tool all read this module, so the README, the CLI and an agent cannot
disagree about what is supported.

The one thing this file will not do is round up. `verified: false` means "written against
the vendor's documentation and not exercised against the live API", and it is reported as
such everywhere.
"""

from __future__ import annotations

from . import limits
from .config import Settings
from .providers import registry

STAGES = ("asr", "translate", "tts", "separator", "lipsync")

# Containers this build can read and write, and the audio it can emit. Kept here rather
# than inferred so `probe`/`mux` changes have one place to update.
INPUT_CONTAINERS = (
    "mp4",
    "mkv",
    "mov",
    "webm",
    "m4v",
    "avi",
    "mpeg",
    "mpg",
    "ts",
    "mp3",
    "wav",
    "m4a",
    "aac",
    "flac",
    "ogg",
    "opus",
)
OUTPUT_CONTAINERS = ("mp4", "m4a")
OUTPUT_AUDIO = ("aac", "pcm_s16le (wav, intermediate)")


def _provider_rows(settings: Settings, kind: str) -> list[dict]:
    rows = []
    for name in registry.names(kind):
        provider = registry.create(kind, name, settings)
        spec = provider.spec
        rows.append(
            {
                "name": spec.name,
                "title": spec.title,
                "ready": provider.available,
                "missing": provider.missing(),
                "verified": spec.verified,
                "capabilities": list(spec.capabilities),
                "languages": spec.languages,
                "quality": spec.quality,
                "notes": spec.notes,
                "docs": spec.docs,
            }
        )
    return rows


def _feature(kind: str, settings: Settings, capability: str) -> dict:
    """Which ready provider offers `capability`, if any."""
    for name in registry.names(kind):
        if name == "stub":
            continue  # a test double does not make a feature "supported"
        provider = registry.create(kind, name, settings)
        if capability in provider.spec.capabilities and provider.available:
            return {"supported": True, "provider": name, "verified": provider.spec.verified}
    return {"supported": False, "provider": None, "verified": False}


def snapshot(settings: Settings | None = None) -> dict:
    """The whole picture, in JSON-ready form."""
    settings = settings or Settings()
    providers = {kind: _provider_rows(settings, kind) for kind in STAGES}

    ready = {kind: [row["name"] for row in providers[kind] if row["ready"]] for kind in STAGES}
    auto = {}
    for kind in STAGES:
        try:
            auto[kind] = registry.resolve(kind, settings).name
        except Exception:  # noqa: BLE001 - "nothing ready" is an answer, not an error
            auto[kind] = None

    from .fit import CPS

    features = {
        "diarization": _feature("asr", settings, "diarize"),
        "word_timings": _feature("asr", settings, "word_timings"),
        "voice_cloning": _feature("tts", settings, "voice_clone"),
        "style_control": _feature("tts", settings, "style"),
        "emotion_control": _feature("tts", settings, "emotion"),
        "stem_separation": _feature("separator", settings, "vocals"),
        "lip_sync": _feature("lipsync", settings, "mouth_reshaping"),
        "duration_aware_translation": _feature("translate", settings, "duration_aware"),
        "offline_translation": _feature("translate", settings, "offline"),
        "fallback_enabled": settings.fallback,
        # Reuse contract (0.5.0): what a regeneration can reuse, and what it always rebuilds.
        "segment_reuse": settings.segment_reuse,
        "stem_cache": settings.stem_cache,
        "dialogue_reuse": settings.dialogue_cache,
        "always_rebuilt": ["normalisation", "ducking", "mix", "mux"],
    }

    return {
        "version": _version(),
        "stages": list(STAGES),
        "providers": providers,
        "ready": ready,
        "auto": auto,
        "features": features,
        "languages": {
            "timing_model": sorted(CPS),
            "note": "timing uses a per-language characters-per-second constant; provider language "
            "coverage is per adapter and quoted in each provider's `languages` field",
        },
        "formats": {
            "input_containers": list(INPUT_CONTAINERS),
            "output_containers": list(OUTPUT_CONTAINERS),
            "output_audio": list(OUTPUT_AUDIO),
            "video_stream": "copied, never re-encoded",
            "subtitles": ["srt", "vtt", "json transcript"],
        },
        "limits": limits.describe(settings),
        "optional_dependencies": _optional_dependencies(settings),
        "notes": [
            "`verified: false` means the adapter follows the vendor's documentation and has not "
            "been exercised against the live API in this release.",
            "`ready` means the key, package or binary it needs is present on this machine.",
        ],
    }


def _optional_dependencies(settings: Settings) -> list[dict]:
    seen: dict[str, dict] = {}
    for kind in STAGES:
        for name in registry.names(kind):
            spec = registry.create(kind, name, settings).spec
            for package in spec.packages:
                seen.setdefault(
                    package, {"kind": "package", "name": package, "used_by": [], "installed": None}
                )
                seen[package]["used_by"].append(f"{kind}/{spec.name}")
            for binary in spec.binaries:
                seen.setdefault(
                    binary, {"kind": "binary", "name": binary, "used_by": [], "installed": None}
                )
                seen[binary]["used_by"].append(f"{kind}/{spec.name}")
    import importlib.util
    import shutil

    for entry in seen.values():
        if entry["kind"] == "package":
            try:
                entry["installed"] = importlib.util.find_spec(entry["name"]) is not None
            except (ImportError, ValueError):
                entry["installed"] = False
        else:
            entry["installed"] = bool(shutil.which(entry["name"]))
    return sorted(seen.values(), key=lambda e: e["name"])


def _version() -> str:
    from . import __version__

    return __version__


def text(settings: Settings | None = None) -> str:
    """Human-readable form, for `dubforge capabilities`."""
    data = snapshot(settings)
    lines = [f"dubforge {data['version']} — capabilities", ""]
    for kind in data["stages"]:
        rows = data["providers"][kind]
        ready = [row["name"] for row in rows if row["ready"]]
        auto = data["auto"].get(kind)
        lines.append(f"{kind:<10} auto={auto or 'none'}  ready: {', '.join(ready) or 'none'}")
        for row in rows:
            flags = []
            flags.append("ready" if row["ready"] else "not ready")
            flags.append("verified" if row["verified"] else "unverified")
            lines.append(f"  {row['name']:<16} {', '.join(flags):<22} {row['languages']}")
    lines.append("")
    lines.append("features")
    for name, state in data["features"].items():
        if isinstance(state, dict):
            value = (
                f"yes via {state['provider']} ({'verified' if state['verified'] else 'unverified'})"
                if state["supported"]
                else "no"
            )
        else:
            value = "enabled" if state else "disabled"
        lines.append(f"  {name:<28} {value}")
    lines.append("")
    lines.append(limits.summary(settings or Settings()))
    return "\n".join(lines)
