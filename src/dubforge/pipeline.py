"""The pipeline: probe → separate → transcribe → translate → fit → voice → mix → QC.

One function does the work (`dub`) and one answers "what would that cost and can I
run it" (`plan`). The order matters and is not arbitrary:

1. **Separate first.** The bed is needed no matter what happens to the dialogue, and
   doing it up front means a failure later does not waste the expensive step.
2. **Transcribe with word timings**, then merge into sentences. A dub is built on
   sentences, not on ASR blocks.
3. **Translate against the clock**, then rewrite any line that does not fit. This is
   where a machine translation becomes a dubbing script.
4. **Synthesise one line at a time**, so one bad line costs one line.
5. **Mix, then measure.** QC is not optional; it is the deliverable's conscience.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import alignment, events, ffmpeg, fit, qc, srt, stems, timeline
from . import limits as limits_mod
from . import segments as segment_store
from .config import Settings
from .media import Segment, Transcript, file_fingerprint
from .providers import http as provider_http
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable
from .workspace import WorkspaceError, atomic_write_text, open_job, unlink_retry


class DubError(RuntimeError):
    """The dub cannot continue. The message says what to change."""


class DubCancelled(DubError):
    """A caller asked to stop. Raised between stages and before each paid call.

    Honest about its limits: an ffmpeg subprocess already running and a provider
    request already in flight are *not* interrupted — the check happens at the next
    boundary, which is the next line of dialogue at worst.
    """


@dataclass
class Step:
    name: str
    seconds: float
    detail: str = ""
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "step": self.name,
            "seconds": round(self.seconds, 2),
            "detail": self.detail,
            "ok": self.ok,
        }


@dataclass
class DubPlan:
    source: str
    media: dict
    providers: dict
    stats: dict
    estimates: dict
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "media": self.media,
            "providers": self.providers,
            "stats": self.stats,
            "estimates": self.estimates,
            "problems": self.problems,
            "notes": self.notes,
        }

    @property
    def ready(self) -> bool:
        return not self.problems

    def text(self) -> str:
        m = self.media
        lines = [
            f"source      {self.source}",
            f"            {m.get('duration', 0):.1f}s, "
            f"{m.get('width', 0)}x{m.get('height', 0)}, "
            f"{'video' if m.get('has_video') else 'audio only'}, "
            f"{m.get('channels', 0)}ch {m.get('sample_rate', 0)}Hz",
            "providers   " + ", ".join(f"{k}={v}" for k, v in self.providers.items()),
            "work        " + json.dumps(self.stats, ensure_ascii=False)[:300],
            "estimates   " + json.dumps(self.estimates, ensure_ascii=False)[:300],
        ]
        if self.problems:
            lines.append("PROBLEMS")
            lines += [f"  - {p}" for p in self.problems]
        if self.notes:
            lines.append("notes")
            lines += [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


@dataclass
class DubResult:
    output: str = ""
    audio: str = ""
    workdir: str = ""
    providers_used: dict = field(default_factory=dict)
    retries: list = field(default_factory=list)
    alignment: dict = field(default_factory=dict)
    reused_segments: int = 0
    rendered_segments: int = 0
    stems_reused: int = 0
    stems_rendered: int = 0
    transcript_json: str = ""
    subtitles: dict = field(default_factory=dict)
    qc: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.qc.get("ok"))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "output": self.output,
            "audio": self.audio,
            "workdir": self.workdir,
            "transcript_json": self.transcript_json,
            "providers_used": self.providers_used,
            "retries": self.retries,
            "alignment": self.alignment,
            "review": self.review,
            "reused_segments": self.reused_segments,
            "rendered_segments": self.rendered_segments,
            "stems_reused": self.stems_reused,
            "stems_rendered": self.stems_rendered,
            "subtitles": self.subtitles,
            "qc": self.qc,
            "plan": self.plan,
            "steps": [s.to_dict() for s in self.steps],
            "warnings": self.warnings,
            "report": self.report,
        }


def _log(default):
    return default or (lambda msg: None)


# --------------------------------------------------------------------------- plan


def plan(source: str | Path, settings: Settings | None = None) -> DubPlan:
    """Probe the file, resolve every provider, estimate cost and time. No side effects."""
    settings = settings or Settings()
    source = str(source)
    if not Path(source).exists():
        raise DubError(f"no such file: {source}")
    if not Path(source).is_file():
        raise DubError(f"not a file: {source}")
    invalid = settings.validate()
    if invalid:
        raise DubError("invalid settings:\n  - " + "\n  - ".join(invalid))
    info = ffmpeg.probe(source)
    try:
        limits_mod.check_source(source, settings, duration=info.duration)
    except limits_mod.LimitExceeded as exc:
        raise DubError(f"{exc}\n  {exc.hint}") from exc
    if not info.has_audio:
        raise DubError(f"{source} has no audio track to dub")

    problems: list[str] = []
    notes: list[str] = list(settings.env_warnings)
    chosen: dict[str, str] = {}
    for kind in (
        "asr",
        "translate",
        "tts",
        "separator",
        "lipsync",
    ):
        try:
            provider = registry.resolve(kind, settings)
            chosen[kind] = provider.name
            if provider.spec.kind != "lipsync" and not provider.spec.verified:
                notes.append(
                    f"{kind} provider '{provider.name}' is unverified in this release: "
                    "its adapter is written against vendor docs, so check its first output."
                )
        except ProviderUnavailable as exc:
            problems.append(f"{kind}: {exc}")
            chosen[kind] = "unavailable"

    minutes = info.duration / 60.0
    src_chars = info.duration * fit.cps_for(
        settings.source_lang if settings.source_lang != "auto" else "en"
    )
    tgt_chars = src_chars * 1.1
    from . import pricing

    costs = {
        "asr": pricing.estimate("asr", chosen.get("asr", ""), minutes=minutes),
        "translate": pricing.estimate("translate", chosen.get("translate", ""), chars=src_chars),
        "tts": pricing.estimate("tts", chosen.get("tts", ""), chars=tgt_chars),
    }
    known = [v for v in costs.values() if v is not None]
    estimates = {
        "usd": round(sum(known), 4) if known else None,
        "by_stage_usd": costs,
        "note": "list prices, "
        + pricing.CHECKED
        + "; translation is token-billed and is a rough guess here",
        "minutes": round(minutes, 2),
        "est_translated_chars": int(tgt_chars),
    }
    if any(v is None for v in costs.values()):
        notes.append(
            "at least one stage has no published list price: budget from the vendor dashboard"
        )

    try:
        limits_mod.check_estimate(estimates, settings)
    except limits_mod.LimitExceeded as exc:
        problems.append(f"{exc} — {exc.hint}")

    return DubPlan(
        source=source,
        media=info.to_dict(),
        providers=chosen,
        stats={
            "duration_s": round(info.duration, 2),
            "target_lang": settings.target_lang,
            "source_lang": settings.source_lang,
            "keep_background": settings.keep_background,
            "max_speed": settings.max_speed,
        },
        estimates=estimates,
        problems=problems,
        notes=notes,
    )


# ----------------------------------------------------------------------- helpers


def _pick_voices(settings: Settings, tts, speakers: list[str]) -> dict[str, str]:
    """Explicit map first, then the pool, then whatever the provider offers for the language."""
    pool = list(settings.voice_pool)
    if not pool:
        try:
            pool = [v.id for v in tts.voices(settings.target_lang)][:8]
        except Exception:  # noqa: BLE001 - listing voices is a nicety, not a requirement
            pool = []
    mapping = timeline.map_voices(speakers, pool, fallback=pool[0] if pool else "")
    mapping.update({k: v for k, v in settings.voice_map.items() if v})
    return mapping


CACHE_LOCK_TIMEOUT_S = 10.0
CACHE_LOCK_STALE_S = 120.0


class CacheWriteError(RuntimeError):
    """The transcript cache could not be written safely. Never fatal: the cache is optional."""


# The file lock stops two *processes* racing. It cannot stop two *threads* in one
# process, because they share a pid — and on Windows they then also fight over the
# handle (`PermissionError` on unlink). One lock per cache directory, held by the
# thread that is writing, keeps the file lock doing the job it is actually good at.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(cache_dir: Path) -> threading.Lock:
    key = str(Path(cache_dir).resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _cache_file(cache_dir: Path) -> Path:
    return Path(cache_dir) / "transcript.json"


def _load_cached(cache_dir: Path, fingerprint: str) -> Transcript | None:
    """Read a cache entry, treating anything unexpected as "no entry".

    Reads need no lock: every write is atomic (temp file + `os.replace`), so a reader
    sees either the previous document or the new one, never a half-written file.
    """
    path = _cache_file(cache_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    payload = data.get("transcript")
    if not isinstance(payload, dict) or not payload.get("segments"):
        return None
    try:
        return Transcript.from_dict(payload)
    except (TypeError, KeyError, ValueError):
        return None


def _cache_lock_is_stale(lock: Path) -> bool:
    """A lock is stale when its holder is gone, or when it is implausibly old.

    Windows has no safe pid probe (`os.kill` signals there), so on that platform age
    is the only test — which is why the age threshold is generous.
    """
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return True
    if age > CACHE_LOCK_STALE_S:
        return True
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # Created but not yet written. Live if it is young, debris if it is old.
        return age > 5.0
    pid = info.get("pid") if isinstance(info, dict) else None
    if os.name == "nt" or not isinstance(pid, int) or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _acquire_cache_lock(lock: Path, timeout: float) -> bool:
    """Exclusive create, with stale-lock recovery. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _cache_lock_is_stale(lock):
                lock.unlink(missing_ok=True)
                continue
            time.sleep(0.05)
            continue
        except OSError as exc:
            raise CacheWriteError(f"cannot create a cache lock at {lock}: {exc}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
        return True
    return False


def _save_cached(
    cache_dir: Path,
    fingerprint: str,
    transcript: Transcript,
    timeout: float = CACHE_LOCK_TIMEOUT_S,
) -> str:
    """Write the cache atomically, under a lock, or not at all.

    If the lock cannot be taken, this raises instead of writing unlocked: two writers
    racing on one file is a corrupt cache, and a corrupt cache is a wrong dub later.
    The caller treats the cache as optional and carries on with a warning — the
    transcript is already in memory and in the job directory, so nothing is lost.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / ".lock"
    thread_lock = _thread_lock_for(cache_dir)
    if not thread_lock.acquire(timeout=timeout):
        raise CacheWriteError(
            f"another thread has been writing this cache for more than {timeout:.0f}s"
        )
    try:
        if not _acquire_cache_lock(lock, timeout):
            raise CacheWriteError(
                f"another process has held the cache lock at {lock} for more than {timeout:.0f}s"
            )
        try:
            body = json.dumps(
                {"fingerprint": fingerprint, "transcript": transcript.to_dict()},
                ensure_ascii=False,
                indent=2,
            )
            return str(atomic_write_text(_cache_file(cache_dir), body))
        finally:
            unlink_retry(lock)
    finally:
        thread_lock.release()


def _validate_output(path: Path, source_info) -> None:
    """A finished mux must be readable, non-empty, and contain the dub.

    Publishing a file that cannot be probed is how a broken run looks like a successful one,
    so this is checked *before* the temporary file is moved into place.
    """
    if not path.exists() or path.stat().st_size < 1024:
        raise DubError(f"the muxed output is missing or empty: {path}")
    try:
        info = ffmpeg.probe(str(path))
    except ffmpeg.FFmpegError as exc:
        raise DubError(f"the muxed output cannot be read back: {exc}") from exc
    if not info.has_audio:
        raise DubError("the muxed output has no audio stream")
    if source_info is not None and source_info.has_video and not info.has_video:
        raise DubError("the muxed output lost the video stream")
    if (
        info.duration
        and source_info is not None
        and source_info.duration
        and abs(info.duration - source_info.duration) > 5.0
    ):
        raise DubError(
            f"the muxed output is {info.duration:.1f}s against a "
            f"{source_info.duration:.1f}s source — refusing to publish it"
        )


def _separator_name(settings: Settings) -> str:
    """The separator identity for the stem cache key (the name it would resolve to)."""
    if not settings.keep_background:
        return "none"
    try:
        return registry.resolve_chain("separator", settings)[0].name
    except (ProviderUnavailable, ProviderError):
        return settings.separator or "auto"


def _load_supplied_transcript(path: str | Path) -> Transcript:
    """Load a transcript the caller already has: a JSON dump, or a plain `.srt`."""
    source = Path(path).expanduser()
    if not source.exists():
        raise DubError(f"no transcript at {source}")
    if source.suffix.lower() in (".srt", ".vtt"):
        segments = srt.read_srt(source)
        if not segments:
            raise DubError(f"{source} has no subtitle cues")
        return Transcript(
            segments=segments,
            language="unknown",
            provider=f"supplied-{source.suffix.lstrip('.')}",
            speakers=timeline.speakers_in(segments),
            duration=max(seg.end for seg in segments),
        )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise DubError(f"{source} is not valid JSON: {exc}") from exc
    if isinstance(data, dict) and "transcript" in data:  # a cache file
        data = data["transcript"]
    if not isinstance(data, dict) or "segments" not in data:
        raise DubError(f"{source} does not look like a transcript (no 'segments')")
    transcript = Transcript.from_dict(data)
    if not transcript.segments:
        raise DubError(f"{source} has no segments")
    return transcript


def regenerate(
    job_id: str,
    settings: Settings | None = None,
    edits: dict[str, str] | None = None,
    output: str | Path | None = None,
    log=None,
) -> DubResult:
    """Re-dub a finished job with some lines edited, reusing every untouched render.

    `edits` maps a segment id (or its index) to new target text. Everything not edited is
    reused from the segment store, so changing one sentence costs one synthesis rather than a
    whole film. The new dub is written to a **new path** — a previous output is never
    replaced by a run that might fail.
    """
    from .jobs import JobStore

    settings = settings or Settings()
    say = _log(log)
    store = JobStore(settings.workdir)
    job = store.get(job_id)
    if job is None:
        raise DubError(f"no job {job_id!r} in {settings.workdir}")
    if not job.transcript_json or not Path(job.transcript_json).exists():
        raise DubError(
            f"job {job_id} has no transcript to edit; run it again with a transcript written"
        )

    transcript = Transcript.load(job.transcript_json)
    if not transcript.segments:
        raise DubError(f"job {job_id} has an empty transcript")

    applied = 0
    by_id = {segment_store.segment_id(seg): seg for seg in transcript.segments}
    for key, text in (edits or {}).items():
        target = by_id.get(key)
        if target is None and key.isdigit():
            index = int(key)
            target = next((seg for seg in transcript.segments if seg.index == index), None)
        if target is None:
            raise DubError(f"no segment {key!r} in job {job_id}")
        target.target_text = text
        target.translator = "edited"
        applied += 1

    # A new transcript file, written atomically: the original stays as it was.
    base = Path(job.transcript_json)
    version = 1
    while (base.parent / f"transcript.regen-{version}.json").exists():
        version += 1
    edited_path = base.parent / f"transcript.regen-{version}.json"
    from .workspace import atomic_write_text

    atomic_write_text(edited_path, json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2))

    if output:
        destination = Path(output)
    else:
        previous = Path(job.output) if job.output else Path(job.source)
        destination = previous.with_name(f"{previous.stem}.r{version}{previous.suffix or '.mp4'}")
    if destination.resolve() == Path(job.source).resolve():
        raise DubError("the regenerated output would overwrite the source")

    say(f"· regenerate {job_id}: {applied} edited line(s) → {destination.name}")
    run_settings = settings.with_overrides(
        transcript=str(edited_path), retranslate=False, segment_reuse=True
    )
    result = dub(job.source, run_settings, output=destination, log=log)
    result.warnings.append(
        f"regenerated from job {job_id}: {applied} edited line(s), "
        f"{result.rendered_segments} rendered, {result.reused_segments} reused"
    )
    return result


def _write_review(
    result: DubResult, segments: list, settings: Settings, transcript: Transcript, path: Path
) -> str:
    """A stable, machine-readable record for a human reviewer.

    One row per line with everything needed to accept or reject it: the source sentence, the
    translation, the speaker, both timings, the voice, the audio file, the fit verdict, the
    alignment precision, whether it was regenerated, and any warnings. This is the artefact a
    review tool or a spreadsheet should read — the subtitles are for reading, this is for
    deciding.
    """
    from . import fit as fit_module

    rows = []
    for seg in segments:
        verdict = fit_module.fit(
            seg.speech, seg.window or seg.duration or 0.1, settings.target_lang, settings.max_speed
        )
        rows.append(
            {
                "index": seg.index,
                "seg_id": seg.seg_id,
                "speaker": seg.speaker,
                "source_text": seg.text,
                "target_text": seg.speech,
                "translator": seg.translator,
                "voice": seg.voice,
                "source": {"start": round(seg.start, 3), "end": round(seg.end, 3)},
                "target": {
                    "start": round(seg.start, 3),
                    "window": seg.window,
                    "rendered_duration": seg.rendered_duration,
                    "speed": seg.speed,
                },
                "fit": {
                    "verdict": verdict.verdict,
                    "ratio": round(verdict.ratio, 3),
                    "target_chars": verdict.target_chars,
                },
                "audio": seg.audio_path,
                "alignment": alignment.describe(seg),
                "regenerated": seg.regenerated,
                "warnings": list(seg.notes),
            }
        )
    stage_seconds = {step.name: round(step.seconds, 2) for step in result.steps if step.seconds}
    total_segments = max(1, len(segments))
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(settings.workdir and Path(result.output).name),
        "target_lang": settings.target_lang,
        "source_lang": settings.source_lang,
        "providers_used": result.providers_used,
        "alignment": result.alignment,
        "reused_segments": result.reused_segments,
        "rendered_segments": result.rendered_segments,
        "qc": {
            "ok": result.ok,
            "failures": result.qc.get("failures", []),
            "warnings": result.qc.get("warnings", []),
        },
        "run_warnings": result.warnings,
        # Honest accounting: what was reused and what was rebuilt. A remix always rebuilds
        # the dialogue track and the mix, so "5 lines regenerated" never means "5 lines of
        # work" — the numbers below say what actually ran.
        "work": {
            "segments": len(segments),
            "reused_renders": result.reused_segments,
            "rendered_segments": result.rendered_segments,
            "stems_reused": result.stems_reused,
            "stems_rendered": result.stems_rendered,
            "cache_hit_ratio": round(result.reused_segments / total_segments, 3),
            "rebuilt_stages": ["dialogue track", "normalisation", "ducking", "mix", "mux"],
            "stage_seconds": stage_seconds,
        },
        "segments": rows,
        "note": (
            "One row per line, for human review. `alignment.sentence_level_only` means the "
            "timings are per line, not per word — they were not measured per word."
        ),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    # A static page beside it, for a human: no server, no remote assets, no secrets.
    from .review import to_html

    with contextlib.suppress(OSError):
        atomic_write_text(path.with_suffix(".html"), to_html(payload))
    return str(path)


def _write_report(
    result: DubResult, plan_obj: DubPlan, settings: Settings, transcript: Transcript
) -> str:
    lines = [
        "# Dub report",
        "",
        f"* source: `{plan_obj.source}`",
        f"* output: `{result.output}`",
        f"* language: {settings.source_lang} → {settings.target_lang}",
        "* providers: " + ", ".join(f"{k} = {v}" for k, v in plan_obj.providers.items()),
        f"* lines: {len(transcript.segments)}, speakers: {len(timeline.speakers_in(transcript.segments))}",
        "",
        "## QC",
        "",
        "```",
        qc.summary(result.qc),
        "```",
        "",
        "## Steps",
        "",
        "| step | seconds | detail |",
        "| --- | --- | --- |",
    ]
    for step in result.steps:
        lines.append(f"| {step.name} | {step.seconds:.1f} | {step.detail.replace('|', '/')} |")
    if result.retries:
        lines += ["", "## Provider retries", ""]
        for retry in result.retries:
            lines.append(
                f"* {retry.get('provider')} — attempt {retry.get('attempt')}: {retry.get('reason')}"
            )
    if result.warnings:
        lines += ["", "## Warnings", ""] + [f"* {w}" for w in result.warnings]
    lines += [
        "",
        "## Files",
        "",
        f"* dubbed media: `{result.output}`",
        f"* dubbed audio only: `{result.audio}`",
        f"* transcript + translation: `{result.transcript_json}`",
    ]
    for name, path in result.subtitles.items():
        lines.append(f"* {name}: `{path}`")
    lines += [
        "",
        "Prices in the plan are indicative list prices, not a quote.",
        "",
    ]
    path = Path(result.workdir) / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- dub


def dub(
    source: str | Path,
    settings: Settings | None = None,
    output: str | Path | None = None,
    log=None,
    cancel=None,
    progress=None,
) -> DubResult:
    """Run the whole chain. Raises DubError with an actionable message on a hard stop.

    `cancel` is a callable returning True when the caller wants to stop; it is consulted
    between stages and before every paid call. `progress(stage, done, total)` is called
    as stages complete, which is what lets a job store report something truthful.
    """
    settings = settings or Settings()
    say = _log(log)
    source = str(source)
    started_wall = time.time()
    providers_used: dict[str, str] = {}
    retries: list[dict] = []
    # Every provider retry lands here, so a job can report "this run retried twice"
    # without any adapter growing a return value for it. No secrets: provider, reason,
    # attempt number only.
    provider_http.watch_retries(retries)

    def check_cancel() -> None:
        if cancel is not None and cancel():
            raise DubCancelled("cancelled by the caller before the next stage")

    def report_progress(stage: str, done: int, total: int) -> None:
        if progress is not None:
            # A broken observer must not fail the dub it is watching.
            with contextlib.suppress(Exception):
                progress(stage, done, total)

    # Guard the destination before spending anything: the last step of the pipeline is
    # a mux, and ffmpeg's refusal to write over its own input arrives after every
    # provider has been paid for. Failing here costs nothing.
    if output is not None:
        requested = Path(output).expanduser()
        try:
            same = requested.resolve() == Path(source).resolve()
        except OSError:
            same = False
        if same:
            raise DubError(
                f"output would overwrite the source file ({source}). "
                "Choose a different path, or let dubforge name it for you."
            )
        if requested.exists() and requested.is_dir():
            raise DubError(f"output path is a directory: {requested}")

    plan_obj = plan(source, settings)
    if plan_obj.problems:
        raise DubError(
            "cannot start — the plan has problems:\n  - " + "\n  - ".join(plan_obj.problems)
        )
    if settings.dry_run:
        say("dry run: nothing was rendered")
        return DubResult(
            plan=plan_obj.to_dict(),
            steps=[Step("plan", 0.0, "dry run")],
            warnings=plan_obj.notes,
            report=json.dumps(plan_obj.to_dict(), indent=2),
        )

    info = ffmpeg.probe(source)
    stem = Path(source).stem

    asr_for_key = registry.resolve("asr", settings)
    fingerprint = file_fingerprint(
        source,
        f"asr={asr_for_key.name}:{settings.asr_model}:lang={settings.source_lang}:diarize=1",
    )
    try:
        job = open_job(settings.workdir, source, fingerprint=fingerprint, cache_key=fingerprint)
    except (WorkspaceError, OSError) as exc:
        raise DubError(f"cannot prepare a work directory under {settings.workdir}: {exc}") from exc
    work = job.path
    result = DubResult(workdir=str(work), plan=plan_obj.to_dict(), warnings=list(plan_obj.notes))
    result.warnings.extend(job.notes)
    job.write_manifest(output=str(output) if output else "", target_lang=settings.target_lang)

    total_steps = 11

    def step(name: str, fn, *args, **kwargs):
        # Between stages: a cancellation and a wall-clock limit both land here, which is
        # the cheapest place to stop without leaving half-written artefacts.
        check_cancel()
        limits_mod.check_runtime(started_wall, settings)
        started = time.time()
        say(f"· {name}")
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:
            result.steps.append(Step(name, time.time() - started, f"failed: {exc}", ok=False))
            raise
        detail = out[1] if isinstance(out, tuple) else ""
        value = out[0] if isinstance(out, tuple) else out
        result.steps.append(Step(name, time.time() - started, str(detail)))
        report_progress(name, len(result.steps), total_steps)
        return value

    def try_stage(kind: str, what: str, fn):
        """Run a stage against a provider chain, falling back only when asked to.

        Fallback is opt-in (`--fallback` / DUBFORGE_FALLBACK) because switching providers
        changes both quality and cost, and an agent must be told when it happened rather
        than discover it in the bill.
        """
        chain = registry.resolve_chain(kind, settings)
        for index, provider in enumerate(chain):
            try:
                value = fn(provider)
            except ProviderError as exc:
                last = index == len(chain) - 1
                if not settings.fallback or last:
                    raise
                nxt = chain[index + 1]
                message = (
                    f"{what}: {provider.name} failed ({exc.kind}); "
                    f"falling back to {nxt.name} — different quality and cost"
                )
                result.warnings.append(message)
                events.emit(
                    events.PROVIDER_FALLBACK,
                    stage=kind,
                    from_provider=provider.name,
                    to_provider=nxt.name,
                    reason=exc.kind,
                )
                say(f"· {message}")
                continue
            providers_used[kind] = provider.name
            return provider, value
        raise DubError(f"{what}: no provider in the chain could run")  # pragma: no cover

    # 1 — decode ---------------------------------------------------------------
    asr_audio = step(
        "extract audio (16 kHz mono)",
        lambda: (ffmpeg.extract_audio(source, work / "source16.wav", 16000, 1), "asr input"),
    )
    full_audio = step(
        "extract audio (48 kHz stereo)",
        lambda: (ffmpeg.extract_audio(source, work / "source48.wav", 48000, 2), "bed source"),
    )

    # 2 — bed ------------------------------------------------------------------
    bed_path: str | None = None
    stems_reused = 0
    stems_rendered = 0
    # The stem cache lives beside the job directories, keyed by content, so two jobs of the
    # same source (Hindi and Tamil, say) separate the bed once between them.
    stem_cache = (
        stems.StemCache(Path(settings.workdir) / "cache" / "stems") if settings.stem_cache else None
    )
    stem_key = None
    if settings.keep_background and stem_cache is not None:
        stem_key = stems.cache_key(
            source_hash=fingerprint,
            separator=_separator_name(settings),
            keep_background=True,
            sample_rate=48000,
        )
        cached_stem = stem_cache.find(stem_key)
        if cached_stem is not None:
            bed_path = stem_cache.restore(cached_stem, work / "stems" / "bed.wav")
            stems_reused = 1
            result.steps.append(
                Step("separate stems", 0.0, f"reused the cached bed ({stem_key[:8]})")
            )
            say(f"· separate stems — reused cache ({stem_key[:8]})")
    if settings.keep_background and bed_path is None:

        def _separate():
            provider, pair = try_stage(
                "separator",
                "separate stems",
                lambda prov: prov.separate(full_audio, str(work / "stems")),
            )
            return (provider, pair), f"separator {provider.name}"

        try:
            separator, separated = step("separate stems", _separate)
            _vocals, bed = separated
            bed_path = bed
            stems_rendered = 1 if bed else 0
            if bed and stem_cache is not None and stem_key:
                with stem_cache.claim(stem_key, wait=settings.segment_lock_wait_s) as owned:
                    if owned:
                        stem_cache.save(
                            stems.StemRecord(
                                key=stem_key,
                                bed="",
                                bed_sha256="",
                                duration_s=float(info.duration or 0.0),
                                source_hash=fingerprint,
                                separator=_separator_name(settings),
                                sample_rate=48000,
                            ),
                            bed,
                        )
            if not bed:
                result.warnings.append(
                    f"separator '{separator.name}' returned no bed; the dub will replace the whole soundtrack"
                )
        except (ProviderError, ffmpeg.FFmpegError) as exc:
            result.warnings.append(f"separation failed, continuing without a bed: {exc}")

    # 3 — transcribe -----------------------------------------------------------
    asr = asr_for_key
    transcript = None
    if settings.transcript:
        # `--transcript` was documented and silently ignored before 0.3.0: the pipeline
        # always ran recognition. Now it loads the file it was given.
        transcript = _load_supplied_transcript(settings.transcript)
        result.steps.append(
            Step(
                "transcribe",
                0.0,
                f"reused the supplied transcript ({len(transcript.segments)} lines, "
                f"{Path(settings.transcript).name})",
            )
        )
        say(f"· transcribe — supplied, {len(transcript.segments)} lines")
    if transcript is None and settings.cache:
        transcript = _load_cached(job.cache_dir, fingerprint)
    if transcript is not None and settings.transcript:
        pass
    elif transcript is not None:
        result.steps.append(
            Step("transcribe", 0.0, f"reused cached transcript ({len(transcript.segments)} lines)")
        )
        say(f"· transcribe — cached, {len(transcript.segments)} lines")
    else:
        check_cancel()
        started = time.time()
        say(f"· transcribe ({asr_for_key.name})")
        asr, transcript = try_stage(
            "asr",
            "transcribe",
            lambda prov: prov.transcribe(asr_audio, settings.source_lang, diarize=True),
        )
        result.steps.append(
            Step(
                "transcribe",
                time.time() - started,
                f"{len(transcript.segments)} lines, {len(transcript.speakers)} speaker(s), lang={transcript.language}",
            )
        )
        # The cache key names the provider that was *asked* for. After a fallback the
        # transcript came from someone else, so caching it under that key would hand a
        # later run the wrong provenance.
        if settings.cache and asr.name == asr_for_key.name:
            try:
                _save_cached(job.cache_dir, fingerprint, transcript)
            except (CacheWriteError, OSError) as exc:
                # Losing the cache costs a future transcription, not this dub.
                result.warnings.append(f"transcript cache not written: {exc}")
                say(f"· cache skipped: {exc}")
        elif settings.cache:
            result.warnings.append(
                f"transcript not cached: produced by {asr.name} after a fallback from {asr_for_key.name}"
            )
        job.mark("transcribe", True, f"{len(transcript.segments)} lines")

    if not transcript.segments:
        raise DubError("no speech was found in the audio. Check the file, or pass --transcript.")
    if transcript.notes:
        result.warnings.extend(transcript.notes)

    segments = transcript.segments
    if settings.max_segment_s and settings.max_segment_chars:
        segments = _resplit(segments, settings.max_segment_s, settings.max_segment_chars)
        transcript.segments = segments
    timeline.windows(segments, info.duration, settings.tail_gap_s)

    # 4 — voices ---------------------------------------------------------------
    tts_chain = registry.resolve_chain("tts", settings)
    tts = tts_chain[0]
    providers_used["tts"] = tts.name
    speakers = timeline.speakers_in(segments)
    voice_map = _pick_voices(settings, tts, speakers)
    for seg in segments:
        seg.voice = voice_map.get(seg.speaker, "")
        seg.style = settings.style or None
    result.steps.append(
        Step(
            "assign voices",
            0.0,
            f"{len(speakers)} speaker(s) → "
            + ", ".join(f"{k}={v or 'default'}" for k, v in voice_map.items()),
        )
    )

    # 5 — translate ------------------------------------------------------------
    same_language = _same_language(settings.source_lang, settings.target_lang) or _same_language(
        transcript.language, settings.target_lang
    )
    keep_existing = (
        not settings.retranslate
        and bool(segments)
        and all((seg.target_text or "").strip() for seg in segments)
    )
    translator = None
    if keep_existing:
        # Regeneration: the caller edited these lines, so translating again would throw the
        # edits away. Say so in the report rather than silently keeping them.
        result.steps.append(Step("translate", 0.0, "kept the supplied translations (regeneration)"))
        result.warnings.append("translations reused as supplied; no translator was called")
        for seg in segments:
            seg.translator = seg.translator or "supplied"
    elif same_language:
        for seg in segments:
            seg.target_text = seg.text
            seg.translator = "same-language"
        result.steps.append(
            Step("translate", 0.0, "source and target are the same language; skipped")
        )
    else:
        check_cancel()
        started = time.time()
        say(f"· translate → {settings.target_lang}")
        translator, _ = try_stage(
            "translate",
            "translate",
            lambda prov: prov.translate(
                segments,
                settings.source_lang if settings.source_lang != "auto" else transcript.language,
                settings.target_lang,
                glossary=settings.glossary,
                context=settings.context or settings.style,
            ),
        )
        result.steps.append(
            Step("translate", time.time() - started, f"{translator.name}, {len(segments)} lines")
        )

        # 6 — rewrite loop: the difference between translated and dubbable
        started = time.time()
        rounds = 0
        for _ in range(max(0, settings.rewrite_rounds)):
            over = fit.needs_rewrite(segments, settings.target_lang, settings.max_speed)
            if not over:
                break
            before = sum(len(s.speech) for s in over)
            say(f"· rewrite {len(over)} over-long line(s)")
            instructions = {
                s.index: fit.constraint_note(s, settings.target_lang, settings.max_speed)
                for s in over
            }
            try:
                translator.translate(
                    over,
                    settings.source_lang if settings.source_lang != "auto" else transcript.language,
                    settings.target_lang,
                    glossary=settings.glossary,
                    instructions=instructions,
                    context=settings.context or settings.style,
                )
            except ProviderError as exc:
                result.warnings.append(f"rewrite pass skipped: {exc}")
                break
            rounds += 1
            after = sum(len(s.speech) for s in over)
            if before and after >= before * 0.98:
                result.warnings.append(
                    f"'{translator.name}' did not shorten the lines it was given; timing will be handled by atempo"
                )
                break
        if rounds:
            result.steps.append(
                Step("rewrite for timing", time.time() - started, f"{rounds} round(s)")
            )
        if translator is not None:
            providers_used["translate"] = translator.name

    # 7 — synthesise -----------------------------------------------------------
    tts_dir = work / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    say(f"· synthesise {len(segments)} line(s) ({tts.name})")
    clips: list[tuple[str, float]] = []
    failures: list[str] = []
    active_tts = tts

    def _synth(provider, segment, path):
        """Render one line, preferring a provider that also reports word timings."""
        method = getattr(provider, "synthesize_with_timestamps", None)
        if callable(method):
            return method(
                segment.speech,
                segment.voice or "",
                path,
                style=segment.style,
                emotion=settings.emotion or None,
            )
        return provider.synthesize(
            segment.speech,
            segment.voice or "",
            path,
            style=segment.style,
            emotion=settings.emotion or None,
        )

    store = segment_store.SegmentStore(work / "segments") if settings.segment_reuse else None
    reused_lines = 0
    rendered_lines = 0

    def _place(seg) -> None:
        """Stretch a line into its window and queue it for the mix.

        The stretch depends on the window, which changes when a neighbour changes, so it is
        redone every run — it costs milliseconds, unlike the synthesis it follows.
        """
        window = seg.window or seg.duration
        speed = 1.0
        if seg.raw_duration and window > 0.1 and seg.raw_duration > window * 1.02:
            speed = min(seg.raw_duration / window, settings.max_speed)
            try:
                ffmpeg.stretch(seg.audio_path, seg.audio_path, speed)
            except ffmpeg.FFmpegError as exc:
                result.warnings.append(
                    f"line {seg.index}: could not stretch ({exc}); left at original speed"
                )
                speed = 1.0
        seg.speed = round(speed, 3)
        seg.rendered_duration = ffmpeg.duration_of(seg.audio_path)
        clips.append((seg.audio_path, seg.start))

    for seg in segments:
        check_cancel()  # before every paid call, which is the finest honest boundary
        path = str(tts_dir / f"{seg.index:05d}.wav")
        seg.seg_id = segment_store.segment_id(seg)
        want = segment_store.fingerprint(
            seg,
            provider=tts.name,
            model=settings.tts_model or "",
            language=settings.target_lang,
            sample_rate=48000,
        )

        # Own the segment *before* calling a paid provider, then re-check the cache under
        # that ownership. Two workers that both missed must not both synthesise the line.
        claim = (
            store.claim(seg.seg_id, wait=settings.segment_lock_wait_s)
            if store is not None
            else None
        )
        owned = claim.__enter__() if claim is not None else False
        try:
            cached = store.find(seg.seg_id, want) if store is not None else None
            if cached is not None:
                segment_store.restore(cached, path)
                seg.audio_path = path
                seg.raw_duration = cached.duration_s
                seg.render_fingerprint = want
                seg.regenerated = False
                reused_lines += 1
                _place(seg)
                continue
            if store is not None and not owned:
                # Somebody else is rendering this line and we could not wait for them. Render
                # without the cache: slower, never wrong, and it cannot corrupt their record.
                result.warnings.append(
                    f"line {seg.index}: segment lock busy, rendered without cache reuse"
                )
            try:
                provider, rendered = try_stage(
                    "tts",
                    f"synthesise line {seg.index}",
                    # Bind the loop variables: a late-bound closure would synthesise the last
                    # line's text into the first line's file.
                    lambda prov, _seg=seg, _path=path: _synth(prov, _seg, _path),
                )
                if isinstance(rendered, tuple):  # (SynthResult, [Word, ...]) from a provider
                    rendered, words = rendered
                    if words:
                        alignment.attach_measured(seg, words, provider.name)
                if provider.name != active_tts.name:
                    # The fallback provider does not know the first provider's voice ids.
                    # Re-map from what it offers, and say so — a voice change is audible.
                    active_tts = provider
                    voice_map = _pick_voices(settings, active_tts, speakers)
                    for remaining in segments[seg.index :]:
                        remaining.voice = voice_map.get(remaining.speaker, "")
                    result.warnings.append(
                        f"voices re-assigned from {active_tts.name}: the previous provider "
                        "failed mid-run"
                    )
                # A provider can answer 200 with a zero-byte body or a silent file. Left
                # unchecked, that line "succeeds", the timeline gets a 0s clip, and QC reports
                # a dub with a hole in it that nobody notices until playback.
                if not rendered.path or not Path(rendered.path).exists():
                    raise ProviderError(f"line {seg.index}: provider returned no audio file")
                if Path(rendered.path).stat().st_size < 128:
                    raise ProviderError(
                        f"line {seg.index}: provider returned an empty audio file "
                        f"({Path(rendered.path).stat().st_size} bytes)"
                    )
                measured = ffmpeg.duration_of(rendered.path)
                if measured is None or measured < 0.02:
                    raise ProviderError(
                        f"line {seg.index}: provider returned {measured or 0:.3f}s of audio — "
                        "nothing to place"
                    )
                seg.audio_path = rendered.path
                seg.raw_duration = rendered.duration or measured
                seg.render_fingerprint = want
                seg.regenerated = True
                rendered_lines += 1
                if store is not None and owned:
                    # Cache the *raw* render, before the window-specific stretch.
                    store.save(
                        segment_store.RenderRecord(
                            seg_id=seg.seg_id,
                            fingerprint=want,
                            audio="",
                            audio_sha256="",
                            duration_s=float(seg.raw_duration or measured),
                            provider=provider.name,
                            voice=seg.voice or "",
                            model=settings.tts_model or "",
                            language=settings.target_lang,
                            target_text=seg.speech,
                            style=seg.style or "",
                            emotion=settings.emotion or "",
                            sample_rate=48000,
                            timing={"start": seg.start, "end": seg.end, "window": seg.window},
                            meta={"words": len(seg.target_words), "alignment": seg.alignment},
                        ),
                        seg.audio_path,
                    )
            except (ProviderError, ffmpeg.FFmpegError, OSError) as exc:
                failures.append(f"line {seg.index}: {exc}")
                continue
            _place(seg)
        finally:
            if claim is not None:
                claim.__exit__(None, None, None)
    result.stems_reused = stems_reused
    result.stems_rendered = stems_rendered
    result.steps.append(
        Step(
            "synthesise",
            time.time() - started,
            f"{len(clips)}/{len(segments)} lines, {tts.name}, "
            f"{rendered_lines} rendered / {reused_lines} reused",
        )
    )
    result.reused_segments = reused_lines
    result.rendered_segments = rendered_lines
    if not clips:
        raise DubError(
            "no line could be synthesised. First error:\n  "
            + (failures[0] if failures else "unknown")
            + "\nCheck the provider key, the voice id, and the target language."
        )
    if failures:
        result.warnings.append(
            f"{len(failures)} line(s) failed to synthesise: " + "; ".join(failures[:3])
        )
    job.mark("synthesise", not failures, f"{len(clips)}/{len(segments)} lines")

    # 8 — assemble and mix -----------------------------------------------------
    dub_track = step(
        "assemble dub track",
        lambda: (
            ffmpeg.assemble_dub(clips, work / "dub.wav", info.duration, 48000),
            f"{len(clips)} clips placed",
        ),
    )
    normalised = step(
        "normalise loudness",
        lambda: (
            ffmpeg.loudnorm(
                dub_track, work / "dub_norm.wav", settings.loudness_lufs, settings.true_peak_db
            ),
            f"{settings.loudness_lufs} LUFS",
        ),
    )
    if bed_path and Path(bed_path).exists():
        mixed = step(
            "mix with original bed",
            lambda: (
                ffmpeg.duck_and_mix(
                    normalised,
                    bed_path,
                    work / "mix.wav",
                    voice_gain_db=settings.voice_gain_db,
                    bed_gain_db=settings.bed_gain_db,
                    duck=settings.duck,
                    total_duration=info.duration,
                ),
                f"bed {settings.bed_gain_db} dB, ducking {'on' if settings.duck else 'off'}",
            ),
        )
        bed_used = True
    else:
        mixed = normalised
        bed_used = False

    # 9 — mux ------------------------------------------------------------------
    if output is None:
        suffix = ".mp4" if info.has_video else ".m4a"
        output = str(Path(source).with_name(f"{stem}.{settings.target_lang}{suffix}"))
    output = str(output)
    # An explicit --out may point into a folder that does not exist yet; creating it here
    # turns "ffmpeg failed (254)" into a working run.
    try:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DubError(f"cannot create the output folder {Path(output).parent}: {exc}") from exc
    # 11 — publish: mux to a temporary destination, validate it, then move it into place.
    # A failed mux must not destroy the file it was replacing.
    destination = Path(output)
    partial = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
    if destination.exists() and not settings.overwrite_output:
        raise DubError(
            f"{destination} already exists; pass --overwrite (or DUBFORGE_OVERWRITE_OUTPUT=1) "
            "to replace it, or choose another --out"
        )
    try:
        if info.has_video:
            step(
                "mux video + dub",
                lambda: (
                    ffmpeg.mux(source, mixed, str(partial), settings.audio_bitrate),
                    "video stream copied",
                ),
            )
        else:
            step(
                "encode audio",
                lambda: (ffmpeg.mux_audio_only(mixed, str(partial), settings.audio_bitrate), "aac"),
            )

        # 10 — optional lip sync, still into the temporary file -----------------
        if settings.lipsync and settings.lipsync != "none":
            lipsync = registry.resolve("lipsync", settings)
            try:
                synced = step(
                    f"lip sync ({lipsync.name})",
                    lambda: (
                        lipsync.sync(str(partial), str(partial), str(work / "synced.mp4")),
                        lipsync.name,
                    ),
                )
                if synced and Path(synced).exists() and Path(synced) != partial:
                    shutil.copy(synced, partial)
            except ProviderError as exc:
                result.warnings.append(f"lip sync skipped: {exc}")

        # The output is only *published* once the file is complete and readable.
        _validate_output(partial, info)
        os.replace(partial, destination)
    except Exception:
        with contextlib.suppress(OSError):
            partial.unlink()  # an incomplete mux must not survive as a plausible file
        raise
    output = str(destination)

    # 11 — deliverables --------------------------------------------------------
    # Record what ran and what was retried *before* the report is written, so the report
    # and the job document say the same thing.
    alignment_summary = alignment.coverage(segments)
    result.alignment = alignment_summary
    if alignment_summary["measured"] == 0 and segments:
        result.warnings.append(
            "sentence-level timing only: no provider returned word timings, and none were "
            "invented (see docs/quality.md)"
        )
    result.providers_used = dict(providers_used)
    result.retries = list(retries)
    if retries:
        for retry in retries:
            events.emit(
                events.PROVIDER_RETRY,
                provider=retry.get("provider"),
                reason=retry.get("reason"),
                attempt=retry.get("attempt"),
            )
        result.warnings.append(
            f"{len(retries)} provider retr{'y' if len(retries) == 1 else 'ies'} "
            f"(rate limits or server errors; no request was retried after an ambiguous timeout)"
        )
    result.output = output
    result.audio = str(Path(work) / "dub_norm.wav")
    result.transcript_json = transcript.save(work / "transcript.dub.json")
    result.subtitles = srt.write_all(segments, work, stem, lang=settings.target_lang)
    result.qc = qc.report(
        segments,
        dub_audio=normalised,
        mixed_audio=mixed,
        original=source,
        output=output,
        target_lang=settings.target_lang,
        max_speed=settings.max_speed,
        loudness_target=settings.loudness_lufs,
        bed_used=bed_used,
        source_info=info,
        max_chars_per_line=settings.qc_max_chars_per_line,
        max_lines=settings.qc_max_lines,
        max_reading_cps=settings.qc_max_reading_cps,
        min_display_s=settings.qc_min_display_s,
        min_tts_ratio=settings.qc_min_tts_ratio,
        max_audible_speed=settings.qc_max_audible_speed,
        line_silence_ratio=settings.qc_line_silence_ratio,
        silence_sample=settings.qc_silence_sample,
    )
    qc.save(result.qc, work / "qc.json")
    result.review = _write_review(result, segments, settings, transcript, work / "review.json")
    result.report = _write_report(result, plan_obj, settings, transcript)
    job.finished(
        providers_used=result.providers_used,
        provider_retries=len(retries),
        output=output,
        qc_ok=result.qc.get("ok"),
        failures=result.qc.get("failures", []),
        warnings=result.qc.get("warnings", []),
        segments=len(segments),
        rendered=len(clips),
    )
    say(f"· done: {output}")
    if not settings.keep_work:
        removed = job.cleanup(keep=("transcript.dub.json", "qc.json", "report.md"))
        if removed:
            say(f"· cleaned intermediates: {', '.join(removed)}")
    job.release()
    return result


def _same_language(a: str, b: str) -> bool:
    if not a or not b or a == "auto" or b == "auto":
        return False
    return a.lower().split("-")[0] == b.lower().split("-")[0]


def _resplit(segments: list[Segment], max_seconds: float, max_chars: int) -> list[Segment]:
    """Split lines that are too long for one TTS request, proportionally in time."""
    out: list[Segment] = []
    for seg in segments:
        text = seg.text
        if seg.duration <= max_seconds and len(text) <= max_chars:
            out.append(seg)
            continue
        parts = max(2, int(max(seg.duration / max_seconds, len(text) / max_chars)) + 1)
        words = text.split()
        if len(words) < parts:
            out.append(seg)
            continue
        per = len(words) // parts
        span = seg.duration / parts
        for i in range(parts):
            chunk = words[i * per : (i + 1) * per] if i < parts - 1 else words[i * per :]
            if not chunk:
                continue
            out.append(
                Segment(
                    index=len(out),
                    start=round(seg.start + i * span, 3),
                    end=round(seg.start + (i + 1) * span, 3),
                    text=" ".join(chunk),
                    speaker=seg.speaker,
                )
            )
    for i, seg in enumerate(out):
        seg.index = i
    return out
