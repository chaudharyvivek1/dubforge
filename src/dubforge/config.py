"""One settings object, built from flags, a config file or the environment.

Nothing here ever prints a secret. `describe()` reports whether a key is set and
never its value, which is what `dubforge doctor` shows.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

ENV_PREFIX = "DUBFORGE_"

LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")


def is_language_code(code: str, allow_auto: bool = False) -> bool:
    """`hi`, `ta-IN`, `pt-BR` — not `Hindi (India)` and not a sentence."""
    if not code:
        return False
    if allow_auto and code.lower() == "auto":
        return True
    return bool(LANGUAGE_RE.match(code.strip()))


class _BadEnv(ValueError):
    """An environment variable that cannot be coerced to its field's type."""


@dataclass
class Settings:
    # --- language -----------------------------------------------------------
    source_lang: str = "auto"
    target_lang: str = "en"

    # --- providers ----------------------------------------------------------
    asr: str = "auto"
    translator: str = "auto"
    tts: str = "auto"
    separator: str = "auto"
    lipsync: str = "none"

    # --- models (empty means "the adapter's default") -------------------------
    asr_model: str = ""
    translate_model: str = ""
    tts_model: str = ""
    tts_rate: str = "+0%"  # edge-tts only: "+10%" speeds the voice up

    # --- inputs -------------------------------------------------------------
    script: str = ""  # text for the offline stub ASR / demo
    transcript: str = ""  # reuse a .json transcript or a .srt

    # --- timing / quality ---------------------------------------------------
    max_speed: float = 1.15  # fastest atempo we will apply before asking for a rewrite
    min_speed: float = 0.94  # slowest; below this a line gets a pause instead
    rewrite_rounds: int = 2  # how many times an over-long line is sent back
    max_segment_s: float = 12.0
    max_segment_chars: int = 240
    tail_gap_s: float = 0.12  # gap kept between two dub lines

    # --- mix ----------------------------------------------------------------
    keep_background: bool = True
    bed_gain_db: float = -16.0
    voice_gain_db: float = 0.0
    duck: bool = True
    loudness_lufs: float = -16.0
    true_peak_db: float = -1.5
    audio_bitrate: str = "192k"

    # --- craft --------------------------------------------------------------
    style: str = ""  # global delivery hint, e.g. "warm documentary narrator"
    emotion: str = ""  # global emotion hint passed to TTS that supports it
    glossary: dict[str, str] = field(default_factory=dict)  # keep/pronounce map
    voice_map: dict[str, str] = field(default_factory=dict)  # SPEAKER_00 -> voice id
    voice_pool: list[str] = field(default_factory=list)  # voices to assign in order
    speech_cps: float | None = None  # override fit's chars/second
    context: str = ""  # brief for the translator

    # --- run ----------------------------------------------------------------
    workdir: str = "dubforge-work"
    cache: bool = True
    dry_run: bool = False
    keep_work: bool = True
    timeout_s: int = 600  # per provider HTTP call

    # --- diagnostics (filled by from_env; never set by hand) -------------------
    env_warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def key(self, *env_names: str) -> str | None:
        """First non-empty environment variable among `env_names`."""
        for name in env_names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return None

    def provider_key(self, provider: str) -> str | None:
        """Look up a provider's key by its conventional variable names."""
        upper = provider.upper().replace("-", "_")
        return self.key(f"{ENV_PREFIX}{upper}_API_KEY", f"{upper}_API_KEY")

    def describe(self) -> dict:
        """Settings for a report — glossary and voice map included, secrets never."""
        out = {}
        for f in fields(self):
            out[f.name] = getattr(self, f.name)
        return out

    def with_overrides(self, **kwargs) -> Settings:
        known = {f.name for f in fields(self)}
        clean = {k: v for k, v in kwargs.items() if k in known and v is not None}
        return Settings(**{**self.describe(), **clean})

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        base = cls()
        env_values: dict = {}
        warnings: list[str] = []
        for f in fields(cls):
            var = f"{ENV_PREFIX}{f.name.upper()}"
            raw = os.environ.get(var)
            if raw is None or raw == "":
                continue
            default = getattr(base, f.name)
            try:
                env_values[f.name] = _coerce(raw, default)
            except _BadEnv:
                # A malformed variable must not stop a dub, but it must not be silent
                # either: the warning reaches `doctor` and the run's warnings list.
                warnings.append(
                    f"{var}={raw!r} is not valid for {f.name} "
                    f"(expected {type(default).__name__ if default is not None else 'number, bool or JSON'}); ignored"
                )
        settings = cls(**{**env_values, **{k: v for k, v in overrides.items() if v is not None}})
        settings.env_warnings = warnings
        return settings

    # ------------------------------------------------------------------ checks
    def validate(self) -> list[str]:
        """Problems a user can fix. Empty means the settings are usable."""
        problems: list[str] = []
        if not is_language_code(self.target_lang):
            problems.append(
                f"target language {self.target_lang!r} is not a language code like 'hi' or 'ta-IN'"
            )
        if (
            self.source_lang
            and self.source_lang != "auto"
            and not is_language_code(self.source_lang)
        ):
            problems.append(
                f"source language {self.source_lang!r} is not a language code or 'auto'"
            )
        for name, value, low, high in (
            ("max_speed", self.max_speed, 0.5, 2.0),
            ("min_speed", self.min_speed, 0.5, 1.0),
            ("rewrite_rounds", self.rewrite_rounds, 0, 5),
            ("tail_gap_s", self.tail_gap_s, 0.0, 1.0),
            ("loudness_lufs", self.loudness_lufs, -40.0, -5.0),
            ("true_peak_db", self.true_peak_db, -9.0, 0.0),
            ("bed_gain_db", self.bed_gain_db, -60.0, 6.0),
            ("voice_gain_db", self.voice_gain_db, -60.0, 12.0),
            ("max_segment_s", self.max_segment_s, 1.0, 120.0),
            ("timeout_s", self.timeout_s, 5, 3600),
        ):
            if not low <= value <= high:
                problems.append(f"{name}={value} is outside {low}–{high}")
        if self.max_segment_chars < 20:
            problems.append("max_segment_chars must be at least 20")
        if self.speech_cps is not None and not 1.0 <= self.speech_cps <= 60.0:
            problems.append(f"speech_cps={self.speech_cps} is outside 1–60 characters per second")
        return problems

    @classmethod
    def load(cls, path: str | Path | None, **overrides) -> Settings:
        data: dict = {}
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_env(**{**data, **{k: v for k, v in overrides.items() if v is not None}})


def _coerce(raw: str, default) -> object:
    """Turn an environment string into the type the field already has.

    A field typed `float | None` defaults to `None`, which tells us nothing about the
    intended type — that was a real bug: `DUBFORGE_SPEECH_CPS=12.5` used to arrive as
    the string "12.5" and blow up in the timing arithmetic. For those fields the value
    is parsed as JSON (number, bool, list, object) and only falls back to a string.
    """
    text = raw.strip()
    if isinstance(default, bool):
        return text.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError as exc:
            raise _BadEnv(text) from exc
    if isinstance(default, float):
        try:
            return float(text)
        except ValueError as exc:
            raise _BadEnv(text) from exc
    if isinstance(default, dict):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _BadEnv(text) from exc
        if not isinstance(value, dict):
            raise _BadEnv(text)
        return value
    if isinstance(default, list):
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(default, str):
        return raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw


def workdir_for(settings: Settings, name: str = "job") -> Path:
    """Legacy helper: `<workdir>/<name>`.

    Kept for compatibility, but the pipeline no longer uses it — see
    `dubforge.workspace.open_job`, which isolates jobs by source path and locks them.
    """
    path = Path(settings.workdir) / name
    path.mkdir(parents=True, exist_ok=True)
    return path
