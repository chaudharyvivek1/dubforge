"""`dubforge explain` — the cost story of a finished run, in one screen.

Every stage in this pipeline can now be reused, and the honest question a user has after a
regeneration is "what did that actually cost, and what would cost me more if I edited another
line?". `review.json` already carries the answer as data; this module turns it into prose a
person can read, and refuses to invent numbers it does not have.

Three things it will not do:

* claim a stage was reused when the report says it was rebuilt;
* print a percentage that a reader could mistake for time saved on a long film (the demo's
  numbers are not your numbers);
* guess. A job without a `review.json` gets an explanation of why, not a table of zeroes.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .jobs import JobStore

# What a single edited line costs, per stage, given how this release works. Kept beside the
# code rather than in prose so `explain` and the docs cannot drift apart.
NEXT_EDIT = {
    "synthesis": "1 provider call for the edited line; every other line is reused",
    "bed": "not re-separated unless the source or separator changed",
    "dialogue track": "spliced — the edited line's own range is replaced, in one pass",
    "mix": "rebuilt: the loudness gain is global, so the whole mix is recomputed",
    "mux": "rebuilt: the audio encode follows a new mix",
    "qc": "re-run on the new output",
}


class ExplainError(RuntimeError):
    """A job that cannot be explained, with the reason a user can act on."""


def explain(job_id: str, settings: Settings | None = None) -> dict:
    """Read a job's review.json and return the cost story as structured data."""
    settings = settings or Settings()
    store = JobStore(settings.workdir)
    job = store.get(job_id)
    if job is None:
        raise ExplainError(f"no job {job_id!r} in {settings.workdir}")
    if not job.review or not Path(job.review).exists():
        raise ExplainError(
            f"job {job_id} has no review.json yet — a finished run writes one, and a run that "
            "failed before the mix stage will not have one"
        )
    payload = json.loads(Path(job.review).read_text(encoding="utf-8"))
    work = payload.get("work") or {}

    stage_seconds = work.get("stage_seconds") or {}
    total = work.get("total_stage_seconds") or 0.0
    reused_stages = [
        name
        for name, mode in (
            ("extracted audio", work.get("extract_mode")),
            ("dialogue track", work.get("dialogue_mode")),
            ("mix", work.get("mix_mode")),
            ("mux", work.get("mux_mode")),
        )
        if mode == "reused"
    ]
    return {
        "job_id": job.id,
        "status": job.status,
        "source": job.source,
        "target_lang": job.target_lang,
        "qc_ok": (payload.get("qc") or {}).get("ok"),
        "qc_failures": (payload.get("qc") or {}).get("failures") or [],
        "segments": work.get("segments", 0),
        "renders": {
            "reused": work.get("reused_renders", 0),
            "rendered": work.get("rendered_segments", 0),
            "placed_reused": work.get("placed_reused", 0),
            "cache_hit_ratio": work.get("cache_hit_ratio", 0.0),
        },
        "modes": {
            "extract": work.get("extract_mode", "unknown"),
            "dialogue": work.get("dialogue_mode", "unknown"),
            "mix": work.get("mix_mode", "unknown"),
            "mux": work.get("mux_mode", "unknown"),
            "stems_reused": work.get("stems_reused", 0),
        },
        "rebuilt_stages": work.get("rebuilt_stages") or [],
        "timing": {
            "stage_seconds": stage_seconds,
            "total_stage_seconds": total,
            "slowest_stage": work.get("slowest_stage", ""),
            "slowest_seconds": work.get("slowest_seconds", 0.0),
        },
        "reused_stages": reused_stages,
        "next_edit": NEXT_EDIT,
        "note": (
            "Seconds are per stage on the machine that ran this job. A no-op regeneration reuses "
            "every stage and writes a byte-identical file; a one-line edit re-synthesises one "
            "line, splices the dialogue track and rebuilds the mix."
        ),
    }


def render(story: dict) -> str:
    """The same data as a few lines a person reads."""
    modes = story["modes"]
    renders = story["renders"]
    timing = story["timing"]
    lines = [
        f"job {story['job_id']}  ({story['status']}, QC "
        f"{'passed' if story['qc_ok'] else 'failed'})",
        f"  source    {story['source']}  →  {story['target_lang']}",
        f"  lines     {story['segments']}  "
        f"({renders['reused']} reused, {renders['rendered']} rendered, "
        f"{renders['placed_reused']} placed from cache)",
        "",
        "  what ran",
        f"    extracted audio   {modes['extract']}",
        f"    separated bed     {'reused' if modes['stems_reused'] else 'rebuilt'}",
        f"    dialogue track    {modes['dialogue']}",
        f"    mix               {modes['mix']}",
        f"    mux               {modes['mux']}",
    ]
    if story["rebuilt_stages"]:
        lines.append(f"    rebuilt           {', '.join(story['rebuilt_stages'])}")
    else:
        lines.append("    rebuilt           nothing — every stage was reused")

    if timing["stage_seconds"]:
        lines += ["", "  where the time went"]
        for name, seconds in sorted(
            timing["stage_seconds"].items(), key=lambda pair: pair[1], reverse=True
        ):
            share = (
                (seconds / timing["total_stage_seconds"] * 100)
                if timing["total_stage_seconds"]
                else 0
            )
            lines.append(f"    {seconds:6.2f}s  {share:4.0f}%  {name}")
        lines.append(
            f"    {timing['total_stage_seconds']:6.2f}s  total (slowest: "
            f"{timing['slowest_stage'] or 'n/a'})"
        )

    if story["qc_failures"]:
        lines += ["", "  QC failures", *[f"    - {failure}" for failure in story["qc_failures"]]]

    lines += [
        "",
        "  if you edit one more line",
        *[f"    {stage:<15} {what}" for stage, what in story["next_edit"].items()],
        "",
        f"  {story['note']}",
    ]
    return "\n".join(lines)
