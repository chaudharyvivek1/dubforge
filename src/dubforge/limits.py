"""Resource and cost limits: the refusals that stop an agent starting a bad job.

Every limit here is checked *before* the first paid provider call and again where it can
still change the outcome. The point is not to be clever — it is that "this will take four
hours and cost forty dollars" should be a message, not a discovery.

Limits are configured in `Settings` (`DUBFORGE_MAX_INPUT_SECONDS`, …), so a school lab
can set them low and a studio can raise them, and neither has to edit code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


class LimitExceeded(RuntimeError):
    """A configured limit was hit. Carries the numbers and the setting to change."""

    def __init__(self, kind: str, message: str, hint: str = "", **facts):
        super().__init__(message)
        self.kind = kind
        self.hint = hint
        self.facts = facts


@dataclass
class SourceFacts:
    path: str
    bytes: int
    seconds: float

    def to_dict(self) -> dict:
        return {"path": self.path, "bytes": self.bytes, "seconds": round(self.seconds, 2)}


def describe(settings: Settings) -> dict:
    """The configured limits, in the units a human reads them in."""
    return {
        "max_input_bytes": settings.max_input_bytes,
        "max_input_gib": round(settings.max_input_bytes / 1024**3, 2),
        "max_input_seconds": settings.max_input_seconds,
        "max_input_minutes": round(settings.max_input_seconds / 60, 1),
        "max_run_seconds": settings.max_run_seconds,
        "max_estimated_usd": settings.max_estimated_usd,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "max_retries": settings.max_retries,
        "job_retention_days": settings.job_retention_days,
    }


def check_source(
    path: str | Path, settings: Settings, duration: float | None = None
) -> SourceFacts:
    """Refuse a file that is too big or too long, before anything is spent on it."""
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise LimitExceeded(
            "unreadable_source", f"cannot read {target}: {exc}", "Check the path and permissions."
        ) from exc

    if size > settings.max_input_bytes:
        raise LimitExceeded(
            "max_input_bytes",
            f"{target.name} is {size / 1024**3:.2f} GiB, over the {settings.max_input_bytes / 1024**3:.2f} GiB limit",
            "Raise DUBFORGE_MAX_INPUT_BYTES, or trim/split the file first.",
            bytes=size,
            limit=settings.max_input_bytes,
        )

    seconds = duration if duration is not None else 0.0
    if duration is None:
        from . import ffmpeg

        seconds = ffmpeg.duration_of(target)
    if seconds > settings.max_input_seconds:
        raise LimitExceeded(
            "max_input_seconds",
            f"{target.name} runs {seconds / 60:.1f} min, over the {settings.max_input_seconds / 60:.0f} min limit",
            "Raise DUBFORGE_MAX_INPUT_SECONDS, or dub it in parts.",
            seconds=seconds,
            limit=settings.max_input_seconds,
        )
    return SourceFacts(path=str(target), bytes=size, seconds=seconds)


def check_estimate(estimates: dict, settings: Settings) -> None:
    """Refuse a run whose *estimate* is over budget.

    The estimate comes from list prices, so it is a ceiling to reason about, not a bill.
    A provider with no published price contributes nothing, which is why the message says
    where the number came from.
    """
    usd = (estimates or {}).get("usd")
    if usd is None:
        return
    if usd > settings.max_estimated_usd:
        raise LimitExceeded(
            "max_estimated_usd",
            f"the estimate is ${usd:.2f}, over the ${settings.max_estimated_usd:.2f} limit",
            "Raise DUBFORGE_MAX_ESTIMATED_USD, choose cheaper providers, or dub a shorter cut. "
            "This is a list-price estimate, not a quote.",
            usd=usd,
            limit=settings.max_estimated_usd,
        )


def check_runtime(started_at: float, settings: Settings) -> None:
    """Called between stages: a job that has run too long stops rather than running on."""
    elapsed = time.time() - started_at
    if elapsed > settings.max_run_seconds:
        raise LimitExceeded(
            "max_run_seconds",
            f"the run has taken {elapsed / 60:.1f} min, over the {settings.max_run_seconds / 60:.0f} min limit",
            "Raise DUBFORGE_MAX_RUN_SECONDS, or split the input.",
            elapsed_s=round(elapsed, 1),
            limit=settings.max_run_seconds,
        )


def summary(settings: Settings) -> str:
    limits = describe(settings)
    return (
        f"limits: input ≤ {limits['max_input_gib']} GiB / {limits['max_input_minutes']} min, "
        f"run ≤ {settings.max_run_seconds / 60:.0f} min, "
        f"estimate ≤ ${limits['max_estimated_usd']:.2f}, "
        f"{limits['max_concurrent_jobs']} concurrent job(s)"
    )
