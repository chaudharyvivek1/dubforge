"""One settings object, built from flags, a config file or the environment.

Nothing here ever prints a secret. `describe()` reports whether a key is set and
never its value, which is what `dubforge doctor` shows.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

ENV_PREFIX = "DUBFORGE_"

LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")

# The accepted range for every numeric setting, in one place: `validate()` reads it and
# so does the documentation generator, so a limit can never be enforced but undocumented.
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "max_speed": (0.5, 2.0),
    "min_speed": (0.5, 1.0),
    "rewrite_rounds": (0, 5),
    "tail_gap_s": (0.0, 1.0),
    "loudness_lufs": (-40.0, -5.0),
    "true_peak_db": (-9.0, 0.0),
    "bed_gain_db": (-60.0, 6.0),
    "voice_gain_db": (-60.0, 12.0),
    "max_segment_s": (1.0, 120.0),
    "timeout_s": (5, 3600),
    "max_input_bytes": (1, 1 << 40),
    "max_input_seconds": (1.0, 24 * 3600.0),
    "max_run_seconds": (1.0, 24 * 3600.0),
    "max_estimated_usd": (0.0, 10000.0),
    "max_concurrent_jobs": (1, 16),
    "max_retries": (0, 10),
    "job_retention_days": (1, 3650),
    "job_heartbeat_stale_s": (10.0, 7200.0),
    "job_heartbeat_interval_s": (1.0, 600.0),
    "qc_max_chars_per_line": (10, 120),
    "qc_max_lines": (1, 4),
    "qc_max_reading_cps": (5.0, 60.0),
    "qc_min_display_s": (0.2, 10.0),
    "qc_min_tts_ratio": (0.05, 1.0),
    "qc_max_audible_speed": (1.0, 2.0),
    "qc_line_silence_ratio": (0.1, 1.0),
    "qc_silence_sample": (0, 500),
    "segment_lock_wait_s": (0.0, 600.0),
    "stem_cache_max_bytes": (0, 1 << 40),
}


def is_language_code(code: str, allow_auto: bool = False) -> bool:
    """`hi`, `ta-IN`, `pt-BR` — not `Hindi (India)` and not a sentence."""
    if not code:
        return False
    if allow_auto and code.lower() == "auto":
        return True
    return bool(LANGUAGE_RE.match(code.strip()))


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class _BadEnv(ValueError):
    """An environment variable that cannot be coerced to its field's type."""


class ConfigError(ValueError):
    """A configuration file that cannot be used, with a message that says why."""


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

    # --- resource, cost and concurrency limits --------------------------------
    # Every one of these is a refusal, not a warning: they exist so an agent cannot
    # accidentally start a four-hour job or a fifty-dollar one.
    max_input_bytes: int = 4 * 1024**3  # 4 GiB
    max_input_seconds: float = 4 * 3600.0  # 4 hours
    max_run_seconds: float = 4 * 3600.0  # 4 hours of wall clock
    max_estimated_usd: float = 25.0  # estimate ceiling, from list prices
    max_concurrent_jobs: int = 2
    max_retries: int = 2  # per provider HTTP call
    job_retention_days: int = 30

    # --- provider reliability -------------------------------------------------
    fallback: bool = False  # opt-in: try the next provider on failure

    # --- job liveness ---------------------------------------------------------
    # How long a worker may go without a heartbeat before another process may call the
    # job interrupted. Generous on purpose: a provider call can legitimately take minutes.
    job_heartbeat_stale_s: float = 180.0
    job_heartbeat_interval_s: float = 15.0

    # --- segment-level regeneration (0.3.0) -----------------------------------
    # Reuse a line's raw render when its text, voice, provider, model, language, style and
    # sample rate are unchanged. Off only for debugging.
    segment_reuse: bool = True
    # How long a worker waits for another worker to finish the same line before rendering
    # it anyway (slower, never wrong).
    segment_lock_wait_s: float = 30.0
    # Reuse the separated background bed across runs (0.4.0). Separation is the most
    # expensive local stage and depends only on the source and the separator.
    stem_cache: bool = True
    stem_cache_max_bytes: int = 8 * 1024**3
    # Publishing over an existing file is opt-in: a silent overwrite of a good dub is not a
    # convenience, it is data loss.
    overwrite_output: bool = False
    # Splice changed lines into the previous dialogue track instead of rebuilding it (0.5.0).
    dialogue_cache: bool = True
    # Reuse the mix and the mux when nothing that determines them changed (0.6.0).
    stage_cache: bool = True
    # A regeneration keeps the edited translations; a normal run translates.
    retranslate: bool = True

    # --- subtitle readability (QC) --------------------------------------------
    qc_max_chars_per_line: int = 42
    qc_max_lines: int = 2
    qc_max_reading_cps: float = 20.0
    qc_min_display_s: float = 0.8
    # 0.3.0: measurable audio-quality thresholds. Each is a *warning*: they describe a dub
    # worth listening to before shipping, not one that cannot be written.
    qc_min_tts_ratio: float = 0.3  # rendered / estimated speech, below which a render is suspect
    qc_max_audible_speed: float = 1.10  # atempo above this is audible as compression
    qc_line_silence_ratio: float = 0.5  # silence inside one rendered line
    qc_silence_sample: int = 40  # how many lines to measure silence on (cost bound)

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
        for name, (low, high) in NUMERIC_RANGES.items():
            value = getattr(self, name)
            if not low <= value <= high:
                problems.append(f"{name}={value} is outside {low}–{high}")
        if self.max_segment_chars < 20:
            problems.append("max_segment_chars must be at least 20")
        if self.speech_cps is not None and not 1.0 <= self.speech_cps <= 60.0:
            problems.append(f"speech_cps={self.speech_cps} is outside 1–60 characters per second")
        return problems

    @classmethod
    def load(cls, path: str | Path | None, **overrides) -> Settings:
        """Read a JSON settings file, then apply the environment and explicit overrides.

        Every failure here is a `ConfigError` with the file, the key and what was
        expected — a configuration mistake must never surface as a `TypeError` from
        three frames deeper, halfway through a dub.
        """
        data: dict = {}
        if path:
            data = _read_config_file(Path(path))
        merged = {**data, **{k: v for k, v in overrides.items() if v is not None}}
        try:
            settings = cls.from_env(**merged)
        except TypeError as exc:  # unknown key that slipped past validation
            raise ConfigError(f"unexpected configuration key: {exc}") from exc
        problems = settings.validate()
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
        return settings


def _reject_constant(value: str):
    """JSON allows NaN and Infinity; settings do not."""
    raise ConfigError(f"{value} is not a valid number in a configuration file")


def _read_config_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path} must contain a JSON object at the top level, got {type(data).__name__}"
        )

    known = {f.name for f in fields(Settings)} - {"env_warnings"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"{path} has unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )
    bad_types = []
    for key, value in data.items():
        default = getattr(Settings(), key)
        if isinstance(default, bool) and not isinstance(value, bool):
            bad_types.append(f"{key} must be true or false, got {type(value).__name__}")
        elif (
            isinstance(default, int)
            and not isinstance(default, bool)
            and not isinstance(value, int)
        ):
            bad_types.append(f"{key} must be an integer, got {type(value).__name__}")
        elif isinstance(default, float) and not isinstance(value, (int, float)):
            bad_types.append(f"{key} must be a number, got {type(value).__name__}")
        elif isinstance(default, str) and not isinstance(value, str):
            bad_types.append(f"{key} must be a string, got {type(value).__name__}")
        elif isinstance(default, list) and not isinstance(value, list):
            bad_types.append(f"{key} must be a list, got {type(value).__name__}")
        elif isinstance(default, dict) and not isinstance(value, dict):
            bad_types.append(f"{key} must be an object, got {type(value).__name__}")
        elif isinstance(value, float) and not math.isfinite(value):
            bad_types.append(f"{key} must be a finite number, got {value}")
    if bad_types:
        raise ConfigError(f"{path} has invalid values:\n  - " + "\n  - ".join(bad_types))
    return data


def _coerce(raw: str, default) -> object:
    """Turn an environment string into the type the field already has.

    A field typed `float | None` defaults to `None`, which tells us nothing about the
    intended type — that was a real bug: `DUBFORGE_SPEECH_CPS=12.5` used to arrive as
    the string "12.5" and blow up in the timing arithmetic. For those fields the value
    is parsed as JSON (number, bool, list, object) and only falls back to a string.
    """
    text = raw.strip()
    if isinstance(default, bool):
        # A boolean that is not on this list is an error, not `False`. The old
        # `in {"1","true","yes","on"}` turned a typo ("flase", "maybe") into False,
        # which silently switches off caching, background preservation or ducking.
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise _BadEnv(text)
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
