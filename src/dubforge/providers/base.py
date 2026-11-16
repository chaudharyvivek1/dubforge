"""Provider contracts.

Every stage of a dub is a swappable adapter with the same shape, so the pipeline
never knows or cares which vendor is behind it. A provider declares its spec once
and the registry, `doctor`, `plan` and the MCP tool list all read that spec.

`verified` is deliberately honest. An adapter that has only been written against a
vendor's documentation says so, and `list_providers` shows it, because trusting an
unverified adapter's output silently is how a bad dub ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..media import Segment, Transcript


class ProviderError(RuntimeError):
    """A provider refused or failed. Message is safe to show a user: no secrets."""


class ProviderUnavailable(ProviderError):
    """Missing key, missing package or missing binary — with the fix in the message."""


@dataclass
class Spec:
    name: str
    kind: str  # asr | translate | tts | separator | lipsync
    title: str
    env: tuple[str, ...] = ()  # required environment variables
    packages: tuple[str, ...] = ()  # required importable packages
    binaries: tuple[str, ...] = ()  # required executables
    capabilities: tuple[str, ...] = ()
    languages: str = "any"
    verified: bool = False
    quality: str = ""  # honest one-liner: what this is good and bad at
    notes: str = ""
    docs: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["env"] = list(self.env)
        data["packages"] = list(self.packages)
        data["binaries"] = list(self.binaries)
        data["capabilities"] = list(self.capabilities)
        return data


@dataclass
class Voice:
    id: str
    name: str
    language: str = ""
    gender: str = ""
    style: str = ""
    preview: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class SynthResult:
    path: str
    sample_rate: int = 0
    duration: float | None = None
    voice: str = ""
    provider: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Provider:
    spec: Spec

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def kind(self) -> str:
        return self.spec.kind

    # ---------------------------------------------------------------- readiness
    def missing(self) -> list[str]:
        """Exactly what is missing, in the words of the fix."""
        gaps: list[str] = []
        for var in self.spec.env:
            import os

            if not os.environ.get(var, "").strip():
                gaps.append(f"environment variable {var} is not set")
        for pkg in self.spec.packages:
            try:
                __import__(pkg)
            except Exception:  # noqa: BLE001 - any import failure means "not installed"
                gaps.append(f"python package {pkg} is not installed (pip install {pkg})")
        for binary in self.spec.binaries:
            import shutil

            if not shutil.which(binary):
                gaps.append(f"binary {binary} is not on PATH")
        return gaps

    @property
    def available(self) -> bool:
        return not self.missing()

    def require(self) -> None:
        gaps = self.missing()
        if gaps:
            raise ProviderUnavailable(f"provider '{self.spec.name}' cannot run: " + "; ".join(gaps))

    def describe(self) -> dict:
        data = self.spec.to_dict()
        data["available"] = self.available
        data["missing"] = self.missing()
        return data


# --------------------------------------------------------------------------- ASR


class ASRProvider(Provider):
    spec = Spec(name="base-asr", kind="asr", title="Abstract speech recogniser")

    def transcribe(
        self,
        audio: str,
        language: str | None = None,
        diarize: bool = True,
    ) -> Transcript:
        """Return timed segments (and speaker labels when `diarize`)."""
        raise NotImplementedError


# --------------------------------------------------------------------- translation


class Translator(Provider):
    spec = Spec(name="base-translator", kind="translate", title="Abstract translator")

    def translate(
        self,
        segments: list[Segment],
        source: str,
        target: str,
        glossary: dict[str, str] | None = None,
        instructions: dict[int, str] | None = None,
        context: str = "",
    ) -> None:
        """Write `segment.target_text` in place.

        `instructions` maps a segment index to an extra constraint (used by the
        rewrite loop: "this must fit 2.4 seconds"). `context` is a free-form brief —
        the film's subject, tone, names — which measurably improves terminology.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------- TTS


class TTSProvider(Provider):
    spec = Spec(name="base-tts", kind="tts", title="Abstract speech synthesiser")

    def voices(self, language: str = "") -> list[Voice]:
        raise NotImplementedError

    def synthesize(
        self,
        text: str,
        voice: str,
        out_path: str,
        style: str | None = None,
        emotion: str | None = None,
        sample_rate: int = 48000,
    ) -> SynthResult:
        """Render one line to `out_path` (wav preferred; convert if the vendor returns mp3)."""
        raise NotImplementedError


# --------------------------------------------------------------------- separator


class Separator(Provider):
    spec = Spec(name="base-separator", kind="separator", title="Abstract stem separator")

    def separate(self, audio: str, out_dir: str) -> tuple[str | None, str | None]:
        """Return (dialogue_or_none, background) — background keeps music and effects."""
        raise NotImplementedError


# ----------------------------------------------------------------------- lipsync


class LipSync(Provider):
    spec = Spec(name="base-lipsync", kind="lipsync", title="Abstract lip sync")

    def sync(self, video: str, audio: str, out_path: str) -> str:
        raise NotImplementedError
