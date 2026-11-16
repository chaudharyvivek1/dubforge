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

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg, fit, qc, srt, timeline
from .config import Settings, workdir_for
from .media import Segment, Transcript, file_fingerprint
from .providers import registry
from .providers.base import ProviderError, ProviderUnavailable


class DubError(RuntimeError):
    """The dub cannot continue. The message says what to change."""


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
    info = ffmpeg.probe(source)
    if not info.has_audio:
        raise DubError(f"{source} has no audio track to dub")

    problems: list[str] = []
    notes: list[str] = []
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


def _cache_path(work: Path) -> Path:
    return work / "transcript.json"


def _load_cached(work: Path, fingerprint: str) -> Transcript | None:
    path = _cache_path(work)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    return Transcript.from_dict(data.get("transcript") or {})


def _save_cached(work: Path, fingerprint: str, transcript: Transcript) -> None:
    _cache_path(work).write_text(
        json.dumps(
            {"fingerprint": fingerprint, "transcript": transcript.to_dict()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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
) -> DubResult:
    """Run the whole chain. Raises DubError with an actionable message on a hard stop."""
    settings = settings or Settings()
    say = _log(log)
    source = str(source)
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
    work = workdir_for(settings, stem)
    result = DubResult(workdir=str(work), plan=plan_obj.to_dict(), warnings=list(plan_obj.notes))

    def step(name: str, fn, *args, **kwargs):
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
        return value

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
    if settings.keep_background:
        separator = registry.resolve("separator", settings)
        try:
            _vocals, bed = step(
                f"separate stems ({separator.name})",
                lambda: (separator.separate(full_audio, str(work / "stems")), separator.name),
            )
            bed_path = bed
            if not bed:
                result.warnings.append(
                    f"separator '{separator.name}' returned no bed; the dub will replace the whole soundtrack"
                )
        except (ProviderError, ffmpeg.FFmpegError) as exc:
            result.warnings.append(f"separation failed, continuing without a bed: {exc}")

    # 3 — transcribe -----------------------------------------------------------
    asr = registry.resolve("asr", settings)
    fingerprint = file_fingerprint(
        source, f"{asr.name}:{settings.asr_model}:{settings.source_lang}"
    )
    transcript = _load_cached(work, fingerprint) if settings.cache else None
    if transcript is not None:
        result.steps.append(
            Step("transcribe", 0.0, f"reused cached transcript ({len(transcript.segments)} lines)")
        )
        say(f"· transcribe — cached, {len(transcript.segments)} lines")
    else:
        started = time.time()
        say(f"· transcribe ({asr.name})")
        transcript = asr.transcribe(asr_audio, settings.source_lang, diarize=True)
        result.steps.append(
            Step(
                "transcribe",
                time.time() - started,
                f"{len(transcript.segments)} lines, {len(transcript.speakers)} speaker(s), lang={transcript.language}",
            )
        )
        if settings.cache:
            _save_cached(work, fingerprint, transcript)

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
    tts = registry.resolve("tts", settings)
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
    translator = None
    if same_language:
        for seg in segments:
            seg.target_text = seg.text
            seg.translator = "same-language"
        result.steps.append(
            Step("translate", 0.0, "source and target are the same language; skipped")
        )
    else:
        translator = registry.resolve("translate", settings)
        started = time.time()
        say(f"· translate ({translator.name}) → {settings.target_lang}")
        translator.translate(
            segments,
            settings.source_lang if settings.source_lang != "auto" else transcript.language,
            settings.target_lang,
            glossary=settings.glossary,
            context=settings.context or settings.style,
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

    # 7 — synthesise -----------------------------------------------------------
    tts_dir = work / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    say(f"· synthesise {len(segments)} line(s) ({tts.name})")
    clips: list[tuple[str, float]] = []
    failures: list[str] = []
    for seg in segments:
        path = str(tts_dir / f"{seg.index:05d}.wav")
        try:
            rendered = tts.synthesize(
                seg.speech,
                seg.voice or "",
                path,
                style=seg.style,
                emotion=settings.emotion or None,
            )
            seg.audio_path = rendered.path
            seg.raw_duration = rendered.duration or ffmpeg.duration_of(rendered.path)
        except (ProviderError, ffmpeg.FFmpegError) as exc:
            failures.append(f"line {seg.index}: {exc}")
            continue
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
    result.steps.append(
        Step("synthesise", time.time() - started, f"{len(clips)}/{len(segments)} lines, {tts.name}")
    )
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
    if info.has_video:
        step(
            "mux video + dub",
            lambda: (
                ffmpeg.mux(source, mixed, output, settings.audio_bitrate),
                "video stream copied",
            ),
        )
    else:
        step(
            "encode audio",
            lambda: (ffmpeg.mux_audio_only(mixed, output, settings.audio_bitrate), "aac"),
        )

    # 10 — optional lip sync ---------------------------------------------------
    if settings.lipsync and settings.lipsync != "none":
        lipsync = registry.resolve("lipsync", settings)
        try:
            synced = step(
                f"lip sync ({lipsync.name})",
                lambda: (lipsync.sync(output, output, str(work / "synced.mp4")), lipsync.name),
            )
            if synced and Path(synced).exists() and str(synced) != output:
                shutil.copy(synced, output)
        except ProviderError as exc:
            result.warnings.append(f"lip sync skipped: {exc}")

    # 11 — deliverables --------------------------------------------------------
    result.output = output
    result.audio = str(Path(work) / "dub_norm.wav")
    result.transcript_json = transcript.save(work / "transcript.dub.json")
    result.subtitles = srt.write_all(segments, work, stem)
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
    )
    qc.save(result.qc, work / "qc.json")
    result.report = _write_report(result, plan_obj, settings, transcript)
    say(f"· done: {output}")
    if not settings.keep_work:
        _cleanup(work, keep=[Path(output).name])
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


def _cleanup(work: Path, keep: list[str]) -> None:
    """Drop the intermediates, keep anything a user might want back."""
    for pattern in ("*.part*.wav", "tts", "stems"):
        target = work / pattern
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
