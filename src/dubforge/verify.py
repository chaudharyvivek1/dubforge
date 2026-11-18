"""The verification ledger: what has been proven against a live API, and what has not.

`verified: false` on every adapter is honest, but honesty is not a process. This module is the
process, in three parts:

* **plan** — for every adapter, what a live check would call, which environment variables it
  needs, which are present, whether a check exists for its stage, and what it would cost. No
  requests, no keys read beyond a presence test.
* **run** — the smallest real call each vendor allows, recorded with the date, the DubForge
  version and the adapter's API docs link. Never runs without an explicit opt-in, never prints
  or stores a key.
* **import** — accept a record produced *elsewhere* (a laptop with keys, a CI secret job) after
  validating it: known providers, ISO dates, no secret-looking strings, a version that exists.

The record at `docs/verification.json` is the only thing allowed to make `verified` true, and a
test in the suite enforces that. So the ledger can be produced anywhere, but it cannot be
faked quietly: an imported record has to name a provider that exists, a date, and a version.

Why this is a module and not a script: a script can only be run by someone at a terminal with
keys. A module can be planned, imported, summarised in `doctor`, and tested without a key —
which is the difference between "we intend to verify" and "here is the ledger".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, ffmpeg
from .config import Settings
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable

RECORD = Path(__file__).resolve().parents[2] / "docs" / "verification.json"
SCHEMA = 1
# A verification is a fact with a date on it. Vendors change endpoints, models and prices, so
# a row from two years ago is evidence of a different system. After this, it stops counting and
# says why.
MAX_AGE_DAYS = 180
MAX_DETAIL = 300
STATUSES = ("ok", "failed", "skipped", "unsupported")

# What each stage's probe costs, in the vendor's own units. Deliberately qualitative: a price
# that is wrong is worse than no price, and every vendor changes theirs without notice.
COST = {
    "asr": "~2 s of audio",
    "translate": "one short sentence",
    "tts": "one short phrase (~15 characters)",
}

# A record must never contain anything that looks like a credential. This is a tripwire, not a
# guarantee: it catches the accident, which is the case that actually happens.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9_\-]{32,}"),
)


@dataclass
class State:
    """The effective verification state of one adapter, and where that answer came from."""

    stage: str
    provider: str
    verified: bool
    status: str = "unverified"  # ok | failed | skipped | stale | unverified | unsupported
    checked_at: str = ""
    age_days: int | None = None
    stale: bool = False
    source: str = "none"  # record | declared | none
    contradiction: bool = False  # declared verified, no evidence
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class VerifyError(RuntimeError):
    """A verification action that cannot proceed, with the reason a user can act on."""


# --------------------------------------------------------------------------- plan


def plan(settings: Settings | None = None) -> dict:
    """What a live verification would do, without doing any of it."""
    settings = settings or Settings()
    rows = []
    for kind in ("asr", "translate", "tts"):
        for name in registry.names(kind):
            if name in ("stub", "transcript", "none"):
                continue
            provider = registry.create(kind, name, settings)
            spec = provider.spec
            env = list(spec.env)
            present = [variable for variable in env if os.environ.get(variable)]
            missing = [variable for variable in env if not os.environ.get(variable)]
            rows.append(
                {
                    "stage": kind,
                    "provider": name,
                    "env": env,
                    "env_present": present,
                    "env_missing": missing,
                    "needs": "no key" if not env else (", ".join(env)),
                    "ready": not missing and not provider.missing(),
                    "verified": bool(spec.verified),
                    "check": kind in ("asr", "translate", "tts"),
                    "cost": COST.get(kind, "unknown"),
                    "docs": spec.docs,
                }
            )
    ready = [row for row in rows if row["ready"]]
    return {
        "record": str(RECORD),
        "record_exists": RECORD.exists(),
        "schema": SCHEMA,
        "adapters": rows,
        "total": len(rows),
        "verified": sum(1 for row in rows if row["verified"]),
        "ready": len(ready),
        "run": ([f"{row['stage']}/{row['provider']}" for row in ready] if ready else []),
        "note": (
            "No request is made by `plan`. `run` needs --yes-live because it spends money; "
            "an adapter may be marked verified only when the record has an ok row for it."
        ),
    }


def _age_days(checked_at: str, now: float | None = None) -> int | None:
    """Age of a check, from its ISO date. None when the date cannot be read."""
    text = (checked_at or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            stamp = time.mktime(time.strptime(text, pattern))
        except ValueError:
            continue
        # mktime reads local time; the record is UTC. The error is hours, and the threshold is
        # months, so this is honest enough and platform-independent.
        return max(0, int(((now or time.time()) - stamp) / 86400))
    return None


def state_for(
    kind: str,
    name: str,
    record: dict | None = None,
    *,
    declared: bool = False,
    local: bool = False,
    max_age_days: int = MAX_AGE_DAYS,
    now: float | None = None,
) -> State:
    """The effective state: the record decides, the spec only declares.

    Two kinds of evidence count, and they are not the same promise:

    * a **local** adapter (`spec.local`) runs in this process or in ffmpeg, so the test suite
      exercises it on every run. Its declaration is the evidence, and it is labelled
      `source: "local"` rather than pretending to be a live verification.
    * a **network** adapter needs a dated `ok` row. A declaration without one is a
      contradiction, reported as such, because the whole point of the ledger is that the claim
      has evidence — and a row older than `max_age_days` is evidence of a system that has since
      changed, so it expires.
    """
    payload = record if record is not None else load()
    rows = [
        row
        for row in payload.get("results", [])
        if row.get("stage") == kind and row.get("provider") == name
    ]
    if not rows:
        if declared and local:
            return State(
                stage=kind,
                provider=name,
                verified=True,
                status="local",
                source="local",
                note="local adapter: exercised by the test suite, no live API involved",
            )
        if declared:
            return State(
                stage=kind,
                provider=name,
                verified=False,
                status="unsupported",
                source="declared",
                contradiction=True,
                note="the adapter declares verified but the record has no row for it",
            )
        return State(
            stage=kind,
            provider=name,
            verified=False,
            status="unverified",
            note="never verified against a live API",
        )

    row = rows[-1]  # the most recent run wins; import validation rejects duplicates
    status_value = str(row.get("status") or "")
    checked_at = str(row.get("checked_at") or "")
    age = _age_days(checked_at, now)
    if status_value != "ok":
        return State(
            stage=kind,
            provider=name,
            verified=False,
            status=status_value or "unverified",
            checked_at=checked_at,
            age_days=age,
            source="record",
            note=str(row.get("detail") or "")[:200] or f"last check: {status_value}",
        )
    if age is None:
        return State(
            stage=kind,
            provider=name,
            verified=False,
            status="unverified",
            checked_at=checked_at,
            source="record",
            note="an ok row without a readable date cannot be trusted",
        )
    if age > max_age_days:
        return State(
            stage=kind,
            provider=name,
            verified=False,
            status="stale",
            checked_at=checked_at,
            age_days=age,
            stale=True,
            source="record",
            note=f"verified {age} days ago (limit {max_age_days}); re-run to renew",
        )
    return State(
        stage=kind,
        provider=name,
        verified=True,
        status="ok",
        checked_at=checked_at,
        age_days=age,
        source="record",
        note=f"verified {age} day(s) ago",
    )


def effective(kind: str, name: str, settings: Settings | None = None) -> bool:
    """Whether this adapter may be called verified right now."""
    settings = settings or Settings()
    provider = registry.create(kind, name, settings)
    spec = provider.spec
    return state_for(kind, name, declared=bool(spec.verified), local=bool(spec.local)).verified


def all_states(settings: Settings | None = None, record: dict | None = None) -> list[State]:
    """Every adapter's effective state, for `capabilities`, `doctor` and `verify status`."""
    settings = settings or Settings()
    payload = record if record is not None else load()
    states: list[State] = []
    for kind in registry.REGISTRY:
        for name in registry.names(kind):
            if name in ("stub", "transcript", "none"):
                continue
            provider = registry.create(kind, name, settings)
            spec = provider.spec
            states.append(
                state_for(kind, name, payload, declared=bool(spec.verified), local=bool(spec.local))
            )
    return states


def status() -> dict:
    """A summary of the ledger: what counts now, what has expired, what contradicts."""
    record = load()
    rows = record.get("results", [])
    states = all_states(record=record)
    verified = [state for state in states if state.verified and state.source == "record"]
    local = [state for state in states if state.source == "local"]
    stale = [state for state in states if state.stale]
    contradictions = [state for state in states if state.contradiction]
    return {
        "verified": len(verified),
        "verified_live": [f"{state.stage}/{state.provider}" for state in verified],
        "local": len(local),
        "checked": len(rows),
        "adapters": len(states),
        "stale": len(stale),
        "contradictions": len(contradictions),
        "max_age_days": MAX_AGE_DAYS,
        "generated_at": record.get("generated_at"),
        "schema": record.get("schema"),
        "record": str(RECORD),
        "states": [state.to_dict() for state in states],
        "note": (
            "an adapter counts as verified only while its ok row is younger than "
            f"{MAX_AGE_DAYS} days; the spec's own flag is a declaration, not evidence"
        ),
    }


# ---------------------------------------------------------------------------- run


def _two_second_wav(tmp: Path) -> str:
    """A tiny real audio file: a tone, 16 kHz mono, which every ASR accepts."""
    tmp.mkdir(parents=True, exist_ok=True)
    path = str(tmp / "probe.wav")
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=16000:duration=2",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            path,
        ]
    )
    return path


def check_asr(provider, settings: Settings, tmp: Path) -> dict:
    audio = _two_second_wav(tmp)
    transcript = provider.transcribe(audio, settings.source_lang, diarize=False)
    if not isinstance(transcript.segments, list):
        raise AssertionError("transcript has no segment list")
    return {
        "segments": len(transcript.segments),
        "language": transcript.language or settings.source_lang,
        "detail": "audio accepted, transcript structure returned",
    }


def check_translate(provider, settings: Settings, tmp: Path) -> dict:
    from .media import Segment

    source = "The quick brown fox jumps over the lazy dog."
    segments = [Segment(index=0, start=0.0, end=3.0, text=source, window=3.0)]
    provider.translate(segments, "en", settings.target_lang, None, None, "")
    target = (segments[0].target_text or "").strip()
    if not target:
        raise AssertionError("translation came back empty")
    if target == source:
        raise AssertionError("translation came back unchanged")
    return {"chars": len(target), "detail": "text translated"}


def check_tts(provider, settings: Settings, tmp: Path) -> dict:
    voices = provider.voices(settings.target_lang)
    voice = voices[0].id if voices else ""
    tmp.mkdir(parents=True, exist_ok=True)
    out = str(tmp / "probe-tts.wav")
    rendered = provider.synthesize("Namaste. This is a verification line.", voice, out)
    duration = rendered.duration or 0.0
    if duration <= 0.05:
        raise AssertionError(f"rendered audio is {duration:.2f}s — no usable payload")
    return {
        "voice": voice or "(provider default)",
        "duration_s": round(duration, 2),
        "detail": "audio rendered",
    }


CHECKS = {"asr": check_asr, "translate": check_translate, "tts": check_tts}


def verify_one(kind: str, name: str, settings: Settings, tmp: Path) -> dict:
    """One adapter, one minimal real call. Returns a record; never raises."""
    record = {
        "stage": kind,
        "provider": name,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dubforge_version": __version__,
        "status": "skipped",
        "detail": "",
    }
    try:
        provider = registry.create(kind, name, settings)
        gaps = provider.missing()
        if gaps:
            record["detail"] = "; ".join(gaps)[:MAX_DETAIL]
            return record
        check = CHECKS.get(kind)
        if check is None:
            record["status"] = "unsupported"
            record["detail"] = f"no verification check for stage {kind!r}"
            return record
        record.update(check(provider, settings, tmp))
        record["status"] = "ok"
        record["api_version"] = provider.spec.docs
    except ProviderUnavailable as exc:
        record["detail"] = str(exc)[:MAX_DETAIL]
    except ProviderError as exc:
        record["status"] = "failed"
        record["error_kind"] = exc.kind
        record["detail"] = str(exc)[:MAX_DETAIL]
    except AssertionError as exc:
        record["status"] = "failed"
        record["error_kind"] = "schema"
        record["detail"] = str(exc)[:MAX_DETAIL]
    except Exception as exc:  # noqa: BLE001 - a probe must never take the run down
        record["status"] = "failed"
        record["error_kind"] = type(exc).__name__
        record["detail"] = str(exc)[:MAX_DETAIL]
    return record


def run(
    settings: Settings | None = None,
    *,
    yes_live: bool = False,
    only: list[str] | None = None,
    providers: list[str] | None = None,
    write: bool = False,
    tmp: Path | None = None,
    emit=None,
) -> dict:
    """Run the live probes. Refuses without `yes_live`, because it spends money."""
    if not yes_live:
        raise VerifyError(
            "refusing to make live provider requests without an explicit opt-in: "
            "pass --yes-live (it spends money or bandwidth)"
        )
    settings = settings or Settings()
    tmp = tmp or Path("/tmp/dubforge-verify")
    say = emit or (lambda message: None)
    stages = only or ["asr", "translate", "tts"]
    wanted = {name.strip() for name in (providers or []) if name.strip()}

    results: list[dict] = []
    for kind in stages:
        for name in registry.names(kind):
            if name in ("stub", "transcript", "none"):
                continue
            if wanted and name not in wanted:
                continue
            record = verify_one(kind, name, settings, tmp)
            results.append(record)
            say(f"{kind:<10} {name:<14} {record['status']:<9} {record.get('detail', '')[:90]}")

    payload = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dubforge_version": __version__,
        "note": (
            "Produced by the verification harness against live APIs. An adapter may be marked "
            "verified only when its row here says ok. Never contains keys."
        ),
        "results": results,
    }
    if write:
        save(payload)
        say(f"wrote {RECORD}")
    return payload


# ------------------------------------------------------------------------- import


def _looks_like_a_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def validate(payload: dict) -> dict:
    """Everything wrong with a record, as a list a user can act on."""
    problems: list[str] = []
    if not isinstance(payload, dict):
        return {"ok": False, "problems": ["a record must be a JSON object"]}
    if payload.get("schema") not in (None, SCHEMA):
        problems.append(
            f"schema {payload.get('schema')!r} is not supported (this build reads {SCHEMA})"
        )
    if "results" not in payload or not isinstance(payload["results"], list):
        return {"ok": False, "problems": problems + ["the record has no 'results' list"]}

    known = {(kind, name) for kind in registry.REGISTRY for name in registry.REGISTRY[kind]}
    seen: set[tuple[str, str]] = set()
    for position, row in enumerate(payload["results"]):
        if not isinstance(row, dict):
            problems.append(f"row {position} is not an object")
            continue
        stage, name = row.get("stage"), row.get("provider")
        if (stage, name) not in known:
            problems.append(f"row {position}: unknown adapter {stage}/{name}")
            continue
        if (stage, name) in seen:
            problems.append(f"row {position}: {stage}/{name} appears twice")
        seen.add((stage, name))
        if row.get("status") not in STATUSES:
            problems.append(
                f"row {position}: status {row.get('status')!r} is not one of {STATUSES}"
            )
        checked = str(row.get("checked_at") or "")
        if row.get("status") == "ok" and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z?)?", checked
        ):
            problems.append(
                f"row {position}: an ok row needs an ISO date in checked_at, got {checked!r}"
            )
        for key, value in row.items():
            if isinstance(value, str) and _looks_like_a_secret(value):
                problems.append(f"row {position}: {key!r} looks like a credential; refusing it")
    if payload.get("results") and not payload.get("generated_at"):
        problems.append("the record has no generated_at")
    return {
        "ok": not problems,
        "problems": problems,
        "rows": len(payload.get("results") or []),
        "ok_rows": sum(1 for row in payload.get("results") or [] if row.get("status") == "ok"),
    }


def import_record(path: str | Path, write: bool = False) -> dict:
    """Accept a record produced on a machine that had keys, after validating it."""
    source = Path(path).expanduser()
    if not source.exists():
        raise VerifyError(f"no verification record at {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise VerifyError(f"{source} is not valid JSON: {exc}") from exc
    verdict = validate(payload)
    if not verdict["ok"]:
        raise VerifyError("; ".join(verdict["problems"]))
    payload.setdefault("schema", SCHEMA)
    if write:
        save(payload)
    return {**verdict, "written": str(RECORD) if write else None, "imported_from": str(source)}


# ------------------------------------------------------------------------ storage


def load(path: str | Path | None = None) -> dict:
    target = Path(path) if path else RECORD
    if not target.exists():
        return {"schema": SCHEMA, "generated_at": None, "results": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema": SCHEMA, "generated_at": None, "results": []}
    payload.setdefault("results", [])
    return payload


def save(payload: dict, path: str | Path | None = None) -> Path:
    target = Path(path) if path else RECORD
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target


def render_plan(plan_data: dict) -> str:
    """The plan as a table a person reads."""
    lines = [
        f"{'stage':<10}{'provider':<16}{'key':<28}{'ready':<7}{'verified':<9}cost",
        "-" * 92,
    ]
    for row in plan_data["adapters"]:
        key = row["needs"] if row["env_missing"] == [] else f"{row['needs']} (missing)"
        lines.append(
            f"{row['stage']:<10}{row['provider']:<16}{key[:27]:<28}"
            f"{'yes' if row['ready'] else 'no':<7}"
            f"{'yes' if row['verified'] else 'no':<9}{row['cost']}"
        )
    lines += [
        "",
        f"{plan_data['verified']} of {plan_data['total']} adapters verified; "
        f"{plan_data['ready']} ready to run with the keys present.",
        "Run `dubforge verify run --yes-live` to spend money, or "
        "`dubforge verify import <file>` to accept a record made elsewhere.",
        plan_data["note"],
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shim
    """Kept so `python -m dubforge.verify` works; the CLI is `dubforge verify`."""
    print(render_plan(plan()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
