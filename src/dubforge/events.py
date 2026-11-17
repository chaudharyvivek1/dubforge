"""Structured events, off unless you ask for them.

Diagnosing "why did this job take four minutes and then fail" from a status document is
guesswork. With `DUBFORGE_EVENTS=/path/to/events.jsonl` every interesting moment is
appended as one JSON object per line: job created, claimed, stage started, provider retried,
fallback taken, cancelled, recovered, QC verdict, cleanup.

Two rules, both enforced by the tests: an event carries identifiers and durations, never a
key, a header or a transcript; and the log is append-only JSONL so `jq` and `tail -f` both
work without a log library.

Off by default because a library should not write files nobody asked for.
"""

from __future__ import annotations

import json
import os
import threading
import time

_LOCK = threading.Lock()

# The event names are part of the contract: a dashboard or a grep depends on them.
JOB_CREATED = "job.created"
JOB_CLAIMED = "job.claimed"
JOB_STAGE = "job.stage"
JOB_COMPLETED = "job.completed"
JOB_FAILED = "job.failed"
JOB_CANCELLED = "job.cancelled"
JOB_RETRY = "job.retry"
JOB_RECOVERED = "job.recovered"
PROVIDER_RETRY = "provider.retry"
PROVIDER_FALLBACK = "provider.fallback"
QC_RESULT = "qc.result"
CLEANUP = "cleanup"

EVENTS = (
    JOB_CREATED,
    JOB_CLAIMED,
    JOB_STAGE,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_CANCELLED,
    JOB_RETRY,
    JOB_RECOVERED,
    PROVIDER_RETRY,
    PROVIDER_FALLBACK,
    QC_RESULT,
    CLEANUP,
)


def enabled() -> bool:
    return bool(os.environ.get("DUBFORGE_EVENTS"))


def path() -> str | None:
    return os.environ.get("DUBFORGE_EVENTS") or None


def emit(event: str, **fields) -> None:
    """Append one event. A failure to log must never break the work being logged."""
    target = path()
    if not target:
        return
    record = {"event": event, "at": round(time.time(), 3), **fields}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            return


def read(target: str | None = None) -> list[dict]:
    """Read back an event log. Used by the tests and by `dubforge jobs show --events`."""
    source = target or path()
    if not source or not os.path.exists(source):
        return []
    out: list[dict] = []
    with open(source, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
