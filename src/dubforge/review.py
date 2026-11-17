"""The review workflow: export, validate, apply.

`review.json` is written by every run (see `pipeline._write_review`). This module turns it
into a working loop: export a review file for a finished job, let a human edit the
translations, validate what comes back, and apply only the changed lines.

The design constraints that matter:

* **A review file is not a command.** It can change translations, voices and which lines to
  regenerate. It cannot change paths, job ownership, provider credentials or the source — a
  file that tries is rejected, not sanitised.
* **A stale file is refused.** The file carries the job id and the source content
  fingerprint; applying it to a different source, or to a job whose transcript has moved on,
  is an error with an explanation.
* **Applying twice is the same as applying once.** Idempotency is checked, not assumed.
* **Nothing is overwritten.** Applying produces a new output, exactly like `regen`.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .jobs import JobStore

SCHEMA = 1
MAX_TEXT = 5000
# Keys a review file may set. Anything else is either ignored (read-only metadata) or, for
# the ones below, an attempt to reach past the review boundary.
EDITABLE = ("edited_target_text", "voice", "style", "emotion", "regenerate")
FORBIDDEN = (
    "audio",
    "audio_path",
    "claim",
    "token",
    "path",
    "output",
    "source",
    "workdir",
    "provider_key",
    "api_key",
)


class ReviewError(RuntimeError):
    """A review file that cannot be applied, with a message a reviewer can act on."""


def export(job_id: str, settings: Settings | None = None, out: str | Path | None = None) -> dict:
    """Write a review file for a finished job, seeded from the run's own review.json."""
    settings = settings or Settings()
    store = JobStore(settings.workdir)
    job = store.get(job_id)
    if job is None:
        raise ReviewError(f"no job {job_id!r} in {settings.workdir}")
    if not job.review or not Path(job.review).exists():
        raise ReviewError(f"job {job_id} has no review.json yet; a finished run writes one")
    payload = json.loads(Path(job.review).read_text(encoding="utf-8"))

    segments = []
    for row in payload.get("segments", []):
        segments.append(
            {
                "seg_id": row.get("seg_id"),
                "index": row.get("index"),
                "speaker": row.get("speaker"),
                "source_text": row.get("source_text"),
                "target_text": row.get("target_text"),
                "edited_target_text": row.get("target_text"),  # the reviewer edits this
                "voice": row.get("voice"),
                "regenerate": False,
                "source": row.get("source"),
                "target": row.get("target"),
                "fit": row.get("fit"),
                "alignment": row.get("alignment"),
                "regenerated": row.get("regenerated"),
                "audio": row.get("audio"),
                "warnings": row.get("warnings", []),
            }
        )
    review = {
        "schema": SCHEMA,
        "job_id": job.id,
        "source_fingerprint": job.fingerprint,
        "source_path": job.source,
        "target_lang": job.target_lang,
        "providers_used": job.providers_used,
        "transcript_fingerprint": job.request_fingerprint,
        "qc": payload.get("qc", {}),
        "segments": segments,
        "note": (
            "Edit `edited_target_text` (or set `regenerate`) and run "
            "`dubforge review apply <file>`. Paths, tokens and provider keys are not "
            "editable: a file that tries to set them is rejected."
        ),
    }
    if out:
        Path(out).write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return review


def validate(review: dict | str | Path, settings: Settings | None = None) -> dict:
    """Everything wrong with a review file, as a list of actionable problems."""
    settings = settings or Settings()
    problems: list[str] = []
    if not isinstance(review, dict):
        path = Path(review)
        if not path.exists():
            return {"ok": False, "problems": [f"no review file at {path}"]}
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            return {"ok": False, "problems": [f"not valid JSON: {exc}"]}

    if review.get("schema") != SCHEMA:
        problems.append(
            f"schema {review.get('schema')!r} is not supported (this build reads {SCHEMA})"
        )

    job_id = review.get("job_id")
    store = JobStore(settings.workdir)
    job = store.get(job_id) if isinstance(job_id, str) else None
    if job is None:
        problems.append(f"job {job_id!r} does not exist in {settings.workdir}")
        return {"ok": False, "problems": problems}

    if review.get("source_fingerprint") and review["source_fingerprint"] != job.fingerprint:
        problems.append(
            "this review file was made from a different version of the source "
            "(its content fingerprint no longer matches); export a fresh one"
        )

    known = _known_segments(job)
    if known is None:
        problems.append(f"job {job_id} has no transcript to apply edits to")
        return {"ok": False, "problems": problems}

    seen: set[str] = set()
    edits = 0
    for position, row in enumerate(review.get("segments") or []):
        if not isinstance(row, dict):
            problems.append(f"row {position} is not an object")
            continue
        for key in FORBIDDEN:
            if key in row and key not in ("audio", "source"):
                problems.append(f"row {position}: {key!r} is not editable through a review file")
        seg_id = row.get("seg_id")
        if seg_id is None and row.get("index") is not None:
            seg_id = _seg_id_for_index(known, row["index"])
        if not seg_id or seg_id not in known:
            problems.append(f"row {position}: unknown segment {seg_id!r}")
            continue
        if seg_id in seen:
            problems.append(f"row {position}: segment {seg_id} is edited more than once")
            continue
        seen.add(seg_id)

        text = row.get("edited_target_text")
        if text is not None:
            if not isinstance(text, str):
                problems.append(f"row {position}: edited_target_text must be a string")
                continue
            if "\x00" in text:
                problems.append(f"row {position}: edited_target_text contains a NUL byte")
            if len(text) > MAX_TEXT:
                problems.append(
                    f"row {position}: edited_target_text is {len(text)} characters "
                    f"(limit {MAX_TEXT})"
                )
            if text.strip() and text.strip() != (known[seg_id]["target_text"] or "").strip():
                edits += 1
        if row.get("regenerate"):
            edits += 1

    return {
        "ok": not problems,
        "problems": problems,
        "job_id": job_id,
        "edits": edits,
        "segments": len(known),
        "schema": SCHEMA,
    }


def apply(
    review: dict | str | Path, settings: Settings | None = None, output: str | Path | None = None
) -> dict:
    """Apply the edits in a review file, re-rendering only the changed lines."""
    from . import pipeline

    settings = settings or Settings()
    verdict = validate(review, settings)
    if not verdict["ok"]:
        raise ReviewError("; ".join(verdict["problems"]))

    payload = review if isinstance(review, dict) else json.loads(Path(review).read_text("utf-8"))
    known = _known_segments(JobStore(settings.workdir).get(payload["job_id"]))
    edits: dict[str, str] = {}
    voices: dict[str, str] = {}
    for row in payload["segments"]:
        seg_id = row.get("seg_id") or _seg_id_for_index(known, row.get("index"))
        text = row.get("edited_target_text")
        current = (known[seg_id]["target_text"] or "").strip()
        if isinstance(text, str) and text.strip() and text.strip() != current:
            edits[seg_id] = text
        if row.get("voice") and row["voice"] != known[seg_id]["voice"]:
            voices[seg_id] = row["voice"]

    if not edits and not voices:
        return {"applied": 0, "output": None, "note": "nothing changed: applying is a no-op"}

    if voices:
        # A per-line voice override becomes a voice map for that speaker, which is the only
        # granularity the pipeline's voice stage has today.
        overrides = dict(settings.voice_map or {})
        for seg_id, voice in voices.items():
            overrides[known[seg_id]["speaker"]] = voice
        settings = settings.with_overrides(voice_map=overrides)

    result = pipeline.regenerate(payload["job_id"], settings, edits=edits, output=output)
    return {
        "applied": len(edits),
        "voices": len(voices),
        "output": result.output,
        "qc_ok": result.ok,
        "rendered_segments": result.rendered_segments,
        "reused_segments": result.reused_segments,
        "stems_reused": result.stems_reused,
        "review": result.review,
    }


def _known_segments(job) -> dict[str, dict] | None:
    """The job's segments by id, from its transcript, with the current target text."""
    if job is None or not job.transcript_json or not Path(job.transcript_json).exists():
        return None
    from . import segments as segment_store
    from .media import Transcript

    transcript = Transcript.load(job.transcript_json)
    out: dict[str, dict] = {}
    for segment in transcript.segments:
        out[segment_store.segment_id(segment)] = {
            "index": segment.index,
            "speaker": segment.speaker,
            "target_text": segment.speech,
            "voice": segment.voice or "",
            "source_text": segment.text,
        }
    return out


def _seg_id_for_index(known: dict[str, dict], index) -> str | None:
    for seg_id, row in known.items():
        if row["index"] == index:
            return seg_id
    return None


def to_html(review: dict) -> str:
    """A static, self-contained review page: no server, no remote assets, no secrets."""
    from html import escape

    rows = []
    for row in review.get("segments", []):
        edited = row.get("edited_target_text") or ""
        changed = edited.strip() and edited.strip() != (row.get("target_text") or "").strip()
        alignment = row.get("alignment") or {}
        precision = (
            "measured words" if alignment.get("alignment") == "measured" else "sentence-level"
        )
        warnings = "".join(f"<li>{escape(str(w))}</li>" for w in row.get("warnings") or [])
        rows.append(
            "<tr class='{cls}'>"
            "<td>{index}</td><td>{speaker}</td>"
            "<td class='src'>{source}</td><td class='tgt'>{target}</td>"
            "<td>{fit}</td><td>{precision}</td><td>{regen}</td>"
            "<td><ul>{warnings}</ul></td></tr>".format(
                cls="changed" if changed else "",
                index=escape(str(row.get("index"))),
                speaker=escape(str(row.get("speaker"))),
                source=escape(str(row.get("source_text"))),
                target=escape(str(edited or row.get("target_text"))),
                fit=escape(str((row.get("fit") or {}).get("verdict", ""))),
                precision=escape(precision),
                regen="yes" if row.get("regenerated") else "no",
                warnings=warnings,
            )
        )
    qc = review.get("qc") or {}
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>dubforge review — {escape(str(review.get("job_id")))}</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#111}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border-bottom:1px solid #ddd;padding:.35rem .5rem;vertical-align:top;text-align:left}}
 tr.changed{{background:#fff8e1}} .src{{color:#555}}
 ul{{margin:0;padding-left:1rem;color:#a15c00}}
</style></head><body>
<h1>Review: {escape(str(review.get("job_id")))}</h1>
<p>Target <strong>{escape(str(review.get("target_lang")))}</strong> ·
 QC {escape("passed" if qc.get("ok") else "failed")}
 {escape(", ".join(qc.get("failures") or []))}</p>
<p>Highlighted rows differ from the delivered translation. Alignment is stated per line:
"measured words" means the provider reported word timings; "sentence-level" means it did not,
and no word timings were invented.</p>
<table><thead><tr><th>#</th><th>speaker</th><th>source</th><th>translation</th>
<th>fit</th><th>timing</th><th>regenerated</th><th>warnings</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
</body></html>
"""
