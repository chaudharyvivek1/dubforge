"""One settings object, built from flags, a config file or the environment.

Nothing here ever prints a secret. `describe()` reports whether a key is set and
never its value, which is what `dubforge doctor` shows.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

ENV_PREFIX = "DUBFORGE_"


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
        for f in fields(cls):
            raw = os.environ.get(f"{ENV_PREFIX}{f.name.upper()}")
            if raw is None or raw == "":
                continue
            default = getattr(base, f.name)
            try:
                if isinstance(default, bool):
                    env_values[f.name] = raw.strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(default, int):
                    env_values[f.name] = int(raw)
                elif isinstance(default, float):
                    env_values[f.name] = float(raw)
                elif isinstance(default, dict):
                    env_values[f.name] = json.loads(raw)
                elif isinstance(default, list):
                    env_values[f.name] = [x for x in raw.split(",") if x.strip()]
                else:
                    env_values[f.name] = raw
            except (ValueError, json.JSONDecodeError):
                continue  # a malformed env var must not stop a dub
        return cls(**{**env_values, **{k: v for k, v in overrides.items() if v is not None}})

    @classmethod
    def load(cls, path: str | Path | None, **overrides) -> Settings:
        data: dict = {}
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_env(**{**data, **{k: v for k, v in overrides.items() if v is not None}})


def workdir_for(settings: Settings, name: str = "job") -> Path:
    path = Path(settings.workdir) / name
    path.mkdir(parents=True, exist_ok=True)
    return path
