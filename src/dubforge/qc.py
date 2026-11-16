"""Quality control: the report that says whether the dub is actually usable.

Every dub ends with a QC object, and it is not decoration. Each check answers a
question a dubbing editor would ask before approving the file:

* did every line get a voice?
* was it really translated, or did a stub pass it through?
* did any line have to be squeezed until it sounds wrong?
* is one speaker suddenly two people?
* is it loud enough for a phone and quiet enough not to clip?
* does the picture still run for the same length?

A `fail` here means "do not ship this", and the pipeline exits non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ffmpeg
from .media import Segment


def _check(name: str, ok: bool, detail: str, severity: str = "fail") -> dict:
    return {
        "check": name,
        "ok": bool(ok),
        "severity": severity if not ok else "pass",
        "detail": detail,
    }


def report(
    segments: list[Segment],
    dub_audio: str | None = None,
    mixed_audio: str | None = None,
    original: str | None = None,
    output: str | None = None,
    target_lang: str = "",
    max_speed: float = 1.15,
    loudness_target: float = -16.0,
    bed_used: bool = False,
) -> dict:
    checks: list[dict] = []
    rendered = [s for s in segments if s.rendered_duration]
    missing = [s.index for s in segments if not s.rendered_duration]

    checks.append(
        _check(
            "every_line_has_audio",
            not missing,
            "all lines rendered"
            if not missing
            else f"{len(missing)} line(s) have no audio: {missing[:10]}",
        )
    )

    stubbed = [s.index for s in segments if (s.translator or "").startswith("stub")]
    checks.append(
        _check(
            "translated",
            not stubbed,
            "translation ran on every line"
            if not stubbed
            else f"{len(stubbed)} line(s) were not translated (stub translator). "
            "This is a pipeline test, not a dub.",
        )
    )

    empty = [s.index for s in segments if not s.speech.strip()]
    checks.append(
        _check(
            "no_empty_lines",
            not empty,
            "no empty lines" if not empty else f"empty translation on {empty[:10]}",
        )
    )

    over = [s for s in rendered if s.overflow > 0.35]
    worst = max((s.overflow for s in rendered), default=0.0)
    checks.append(
        _check(
            "timing_fits",
            not over,
            f"no line overruns its window by more than 0.35s (worst {worst:.2f}s)"
            if not over
            else f"{len(over)} line(s) overrun: worst {worst:.2f}s on segment {max(over, key=lambda s: s.overflow).index}",
        )
    )

    hot = [s.index for s in rendered if s.speed > max_speed + 1e-6]
    checks.append(
        _check(
            "speech_rate",
            not hot,
            f"no line played faster than {max_speed:.2f}x"
            if not hot
            else f"{len(hot)} line(s) exceeded {max_speed:.2f}x: {hot[:10]}",
        )
    )

    voices: dict[str, set[str]] = {}
    for seg in segments:
        if seg.voice:
            voices.setdefault(seg.speaker, set()).add(seg.voice)
    inconsistent = {spk: sorted(v) for spk, v in voices.items() if len(v) > 1}
    checks.append(
        _check(
            "one_voice_per_speaker",
            not inconsistent,
            f"{len(voices)} speaker(s), one voice each"
            if not inconsistent
            else f"speaker(s) with several voices: {inconsistent}",
        )
    )

    metrics: dict = {
        "segments": len(segments),
        "rendered": len(rendered),
        "speakers": len(voices),
        "chars": sum(len(s.speech) for s in segments),
        "rewrites": sum(s.rewrites for s in segments),
        "max_speed_applied": round(max((s.speed for s in rendered), default=1.0), 3),
        "mean_speed_applied": round(sum(s.speed for s in rendered) / len(rendered), 3)
        if rendered
        else 1.0,
        "worst_overflow_s": round(worst, 3),
        "speech_seconds_original": round(sum(s.duration for s in segments), 2),
        "speech_seconds_dubbed": round(sum(s.rendered_duration or 0 for s in rendered), 2),
    }

    # --- audio measurements ---------------------------------------------------
    probe_target = mixed_audio or dub_audio
    if probe_target and Path(probe_target).exists():
        loud = ffmpeg.measure_loudness(probe_target)
        if loud:
            metrics.update(loud)
            got = loud.get("integrated_lufs")
            if got is not None:
                checks.append(
                    _check(
                        "loudness",
                        abs(got - loudness_target) <= 2.5,
                        f"integrated {got:.1f} LUFS, target {loudness_target:.1f} ±2.5",
                        severity="warn",
                    )
                )
        peak = ffmpeg.peak_db(probe_target)
        if peak is not None:
            metrics["peak_db"] = round(peak, 2)
            checks.append(
                _check(
                    "headroom",
                    peak <= -0.5,
                    f"peak {peak:.2f} dBFS (no clipping)"
                    if peak <= -0.5
                    else f"peak {peak:.2f} dBFS: too hot, may clip",
                    severity="warn",
                )
            )

    # --- duration -------------------------------------------------------------
    if original and output and Path(output).exists():
        try:
            before = ffmpeg.duration_of(original)
            after = ffmpeg.duration_of(output)
            metrics["input_seconds"] = round(before, 2)
            metrics["output_seconds"] = round(after, 2)
            checks.append(
                _check(
                    "duration_preserved",
                    abs(after - before) <= 0.5,
                    f"{before:.2f}s in, {after:.2f}s out",
                    severity="warn",
                )
            )
        except Exception as exc:  # noqa: BLE001 - a failed probe must not lose the report
            metrics["duration_error"] = str(exc)[:200]

    checks.append(
        _check(
            "background_kept",
            bed_used,
            "music and effects bed mixed under the dub"
            if bed_used
            else "no bed: the original soundtrack was not preserved (--no-background or separation unavailable)",
            severity="warn",
        )
    )

    ok = not any(not c["ok"] and c["severity"] == "fail" for c in checks)
    return {
        "ok": ok,
        "target_lang": target_lang,
        "metrics": metrics,
        "checks": checks,
        "failures": [c["check"] for c in checks if not c["ok"] and c["severity"] == "fail"],
        "warnings": [c["check"] for c in checks if not c["ok"] and c["severity"] == "warn"],
    }


def save(qc: dict, path: str | Path) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def summary(qc: dict) -> str:
    """One screen, plain text — what to read before watching the file."""
    m = qc.get("metrics", {})
    lines = [
        f"QC: {'PASS' if qc.get('ok') else 'FAIL'}   target={qc.get('target_lang', '?')}",
        f"  lines        {m.get('rendered', 0)}/{m.get('segments', 0)} rendered, "
        f"{m.get('rewrites', 0)} rewrite(s)",
        f"  timing       worst overrun {m.get('worst_overflow_s', 0):.2f}s, "
        f"max speed {m.get('max_speed_applied', 1):.2f}x (mean {m.get('mean_speed_applied', 1):.2f}x)",
        f"  speech       {m.get('speech_seconds_original', 0):.1f}s source → "
        f"{m.get('speech_seconds_dubbed', 0):.1f}s dubbed",
    ]
    if "integrated_lufs" in m:
        lines.append(
            f"  loudness     {m['integrated_lufs']:.1f} LUFS, peak {m.get('peak_db', 0):.1f} dBFS"
        )
    if "input_seconds" in m:
        lines.append(f"  duration     {m['input_seconds']:.2f}s → {m['output_seconds']:.2f}s")
    for item in qc.get("checks", []):
        mark = "ok  " if item["ok"] else ("WARN" if item["severity"] == "warn" else "FAIL")
        lines.append(f"  [{mark}] {item['check']}: {item['detail']}")
    return "\n".join(lines)
