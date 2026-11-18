"""End-to-end: the offline pipeline must produce a real, QC-passing mp4.

These tests are the ones that would have caught every bug that mattered, because
they run ffmpeg for real: real decode, real silence detection, real atempo, real
mixing, real mux, real measurement.
"""

import json
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, ffmpeg, pipeline
from dubforge.config import Settings


@pytest.fixture()
def demo_settings(tmp_path) -> Settings:
    return Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="ffmpeg_center",
        lipsync="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
        keep_background=True,
    )


@needs_ffmpeg
def test_demo_clip_is_a_real_video(demo_clip):
    info = ffmpeg.probe(demo_clip)
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(demo.TOTAL, abs=0.6)
    assert info.width == 854


@needs_ffmpeg
def test_silence_detection_finds_the_demo_gaps(demo_clip):
    audio = ffmpeg.extract_audio(demo_clip, str(Path(demo_clip).with_suffix(".wav")), 16000, 1)
    spans = ffmpeg.silence_spans(audio)
    assert spans, "the demo clip must have silences, they are what the segmentation keys on"
    assert len(spans) >= 2


@needs_ffmpeg
def test_stub_asr_uses_the_silences_as_boundaries(demo_clip, demo_settings):
    from dubforge.providers.stub import StubASR

    audio = ffmpeg.extract_audio(demo_clip, str(Path(demo_clip).with_name("asr.wav")), 16000, 1)
    transcript = StubASR(demo_settings).transcribe(audio, "en")
    assert len(transcript.segments) == 3
    assert transcript.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert transcript.segments[0].start < transcript.segments[1].start
    assert transcript.segments[0].words, "word timings are what merge_words consumes"
    # the timings must come from the audio's structure, not from an even split
    for segment, expected in zip(transcript.segments, (0.30, 3.30, 5.80), strict=True):
        assert segment.start == pytest.approx(expected, abs=0.15)


@needs_ffmpeg
def test_plan_reports_providers_and_cost(demo_clip, demo_settings):
    plan = pipeline.plan(demo_clip, demo_settings)
    assert plan.ready
    assert plan.providers["asr"] == "stub"
    assert plan.providers["tts"] == "stub"
    assert plan.estimates["minutes"] > 0
    assert "source" in plan.text()


@needs_ffmpeg
def test_full_dub_produces_mp4_audio_and_subtitles(demo_clip, demo_settings, tmp_path):
    out = tmp_path / "dubbed.mp4"
    result = pipeline.dub(demo_clip, demo_settings, output=out)

    assert Path(result.output).exists()
    info = ffmpeg.probe(result.output)
    assert info.has_video and info.has_audio
    # the picture is copied, so the duration must not drift
    assert info.duration == pytest.approx(demo.TOTAL, abs=0.6)

    assert Path(result.audio).exists()
    assert Path(result.transcript_json).exists()
    for path in result.subtitles.values():
        assert Path(path).exists()

    data = json.loads(Path(result.transcript_json).read_text(encoding="utf-8"))
    assert len(data["segments"]) == 3
    assert data["segments"][0]["target_text"]  # the pass-through translator still wrote text


@needs_ffmpeg
def test_qc_fails_a_stub_translation_honestly(demo_clip, demo_settings, tmp_path):
    """A pipeline test must never look like a finished dub."""
    result = pipeline.dub(demo_clip, demo_settings, output=tmp_path / "x.mp4")
    assert result.qc["ok"] is False
    assert "translated" in result.qc["failures"]
    checks = {c["check"]: c for c in result.qc["checks"]}
    assert checks["every_line_has_audio"]["ok"] is True
    assert checks["timing_fits"]["ok"] is True
    assert checks["one_voice_per_speaker"]["ok"] is True


@needs_ffmpeg
def test_dub_without_background_still_works(demo_clip, demo_settings, tmp_path):
    settings = demo_settings.with_overrides(keep_background=False)
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "nobed.mp4")
    checks = {c["check"]: c for c in result.qc["checks"]}
    assert checks["background_kept"]["ok"] is False
    assert checks["background_kept"]["severity"] == "warn"


@needs_ffmpeg
def test_dry_run_renders_nothing(demo_clip, demo_settings, tmp_path):
    settings = demo_settings.with_overrides(dry_run=True)
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "never.mp4")
    assert not (tmp_path / "never.mp4").exists()
    assert result.output == ""
    assert result.plan["providers"]["tts"] == "stub"


@needs_ffmpeg
def test_demo_command_is_self_contained(tmp_path):
    payload = demo.run(tmp_path / "demo", target_lang="hi")
    assert Path(payload["output"]).exists()
    assert payload["qc_ok"] is False  # stub translation is flagged, by design
    assert "tone" in payload["note"]
    assert Path(payload["report"]).exists()


@needs_ffmpeg
def test_stretch_changes_duration_but_keeps_pitch_pipeline(demo_clip, tmp_path):
    audio = ffmpeg.extract_audio(demo_clip, str(tmp_path / "a.wav"), 48000, 1)
    out = str(tmp_path / "b.wav")
    before = ffmpeg.duration_of(audio)
    ffmpeg.stretch(audio, out, 1.25)
    after = ffmpeg.duration_of(out)
    assert after == pytest.approx(before / 1.25, rel=0.05)


def test_atempo_chain_is_within_bounds():
    assert ffmpeg.atempo_chain(1.0) == "atempo=1.000000"
    # every atempo stage must stay inside ffmpeg's 0.5–2.0 window
    for factor in (0.2, 0.4, 0.5, 1.0, 1.9, 2.0, 3.0, 5.5):
        parts = ffmpeg.atempo_chain(factor).split(",")
        values = [float(p.split("=")[1]) for p in parts]
        assert all(0.5 <= v <= 2.0 for v in values)
        product = 1.0
        for v in values:
            product *= v
        assert product == pytest.approx(factor, rel=1e-4)


@needs_ffmpeg
def test_stretch_in_place_is_supported(demo_clip, tmp_path):
    """The pipeline stretches segments in place; ffmpeg cannot do that directly."""
    audio = ffmpeg.extract_audio(demo_clip, str(tmp_path / "in.wav"), 48000, 1)
    before = ffmpeg.duration_of(audio)
    ffmpeg.stretch(audio, audio, 1.2)
    after = ffmpeg.duration_of(audio)
    assert after == pytest.approx(before / 1.2, rel=0.06)
    assert Path(audio).exists()
    assert not (tmp_path / "in.stretch.wav").exists()


# --------------------------------------------------------------------- regressions


@needs_ffmpeg
def test_second_run_reuses_the_cached_transcript(demo_clip, tmp_path):
    """The cache must survive a second run and be keyed by content + ASR settings."""
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=True,
    )
    first = pipeline.dub(demo_clip, settings, output=tmp_path / "one.mp4")
    assert any("cached" in step.detail for step in first.steps) is False
    second = pipeline.dub(demo_clip, settings, output=tmp_path / "two.mp4")
    assert any("cached" in step.detail for step in second.steps), [s.detail for s in second.steps]
    # and the job directory is the same one, resumed rather than duplicated
    assert first.workdir == second.workdir


@needs_ffmpeg
def test_a_changed_asr_configuration_transcribes_again(demo_clip, tmp_path):
    base = {
        "source_lang": "en",
        "target_lang": "hi",
        "translator": "stub",
        "tts": "stub",
        "separator": "none",
        "script": demo.DEMO_SCRIPT,
        "workdir": str(tmp_path / "work"),
        "cache": True,
    }
    pipeline.dub(demo_clip, Settings(asr="stub", **base), output=tmp_path / "a.mp4")
    other = pipeline.dub(
        demo_clip,
        Settings(asr="stub", asr_model="a-different-model", **base),
        output=tmp_path / "b.mp4",
    )
    # Scoped to the transcribe step: since 0.6.0 other stages legitimately say "cached" in
    # their own right (the mix, the mux), and a substring sweep over every step would fail on
    # them for the wrong reason.
    transcribe = [step for step in other.steps if step.name == "transcribe"]
    assert transcribe, "the run must record a transcribe step"
    assert not any("cached" in step.detail for step in transcribe), (
        "a changed asr_model must not reuse the transcript cache"
    )


@needs_ffmpeg
def test_subtitles_are_named_for_the_target_language(demo_clip, tmp_path):
    settings = Settings(
        source_lang="en",
        target_lang="ta",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
    )
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "t.mp4")
    assert result.subtitles["target_srt"].endswith(".ta.srt")
    assert Path(result.subtitles["target_srt"]).exists()


@needs_ffmpeg
def test_manifest_records_a_finished_job(demo_clip, tmp_path):
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
    )
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "m.mp4")
    manifest = json.loads((Path(result.workdir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "done"
    assert manifest["fingerprint"]
    assert manifest["steps"]["transcribe"]["ok"] is True
    assert not (Path(result.workdir) / ".lock").exists(), "the job lock must be released"


@needs_ffmpeg
def test_multiple_audio_tracks_are_reported(demo_clip, tmp_path):
    """A second audio track must not be silently dropped without a word in QC."""
    second = tmp_path / "second.wav"
    ffmpeg.write_tone_sequence(str(second), [(0.0, 9.0, 880.0)], demo.TOTAL)
    multi = tmp_path / "multi.mp4"
    ffmpeg.run(
        [
            "-i",
            demo_clip,
            "-i",
            str(second),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(multi),
        ]
    )
    info = ffmpeg.probe(multi)
    assert info.audio_streams == 2

    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
    )
    result = pipeline.dub(multi, settings, output=tmp_path / "out.mp4")
    checks = {c["check"]: c for c in result.qc["checks"]}
    assert checks["multiple_audio_tracks"]["severity"] == "warn"
    assert result.qc["metrics"]["source_audio_streams"] == 2


@needs_ffmpeg
def test_mix_does_not_clip_with_a_hot_bed(demo_clip, tmp_path):
    hot = tmp_path / "hot.wav"
    ffmpeg.write_tone_sequence(str(hot), [(0.0, demo.TOTAL, 180.0)], demo.TOTAL)
    voice = tmp_path / "voice.wav"
    ffmpeg.write_tone_sequence(str(voice), [(0.0, demo.TOTAL, 440.0)], demo.TOTAL)
    out = tmp_path / "mix.wav"
    ffmpeg.duck_and_mix(
        str(voice),
        str(hot),
        str(out),
        voice_gain_db=6,
        bed_gain_db=6,
        duck=True,
        total_duration=demo.TOTAL,
    )
    peak = ffmpeg.peak_db(out)
    assert peak is not None and peak <= -0.4, f"the limiter did not hold: peak {peak} dBFS"


@needs_ffmpeg
def test_a_provider_that_returns_no_audio_fails_the_line(demo_clip, tmp_path):
    """An empty 200 response must be a failure, not a silent gap in the timeline."""
    from dubforge.providers import registry as real_registry
    from dubforge.providers.base import SynthResult

    class BrokenTTS:
        name = "broken"
        # A Spec always carries `local` since 0.9.0 (the verification ledger reads it), so the
        # double must satisfy the interface rather than rely on a getattr fallback.
        spec = type(
            "S", (), {"name": "broken", "kind": "tts", "verified": False, "local": False}
        )()

        def voices(self, language=""):
            return []

        def synthesize(self, text, voice, out_path, style=None, emotion=None, sample_rate=48000):
            Path(out_path).write_bytes(b"")  # a 200 with nothing in it
            return SynthResult(path=str(out_path), duration=0.0, provider="broken")

    class Shim:
        """Stands in for the registry module; the pipeline resolves *chains* now."""

        def __getattr__(self, item):
            return getattr(real_registry, item)

        def resolve(self, kind, settings=None, requested=None):
            if kind == "tts":
                return BrokenTTS()
            return real_registry.resolve(kind, settings, requested)

        def resolve_chain(self, kind, settings=None, requested=None):
            if kind == "tts":
                return [BrokenTTS()]
            return real_registry.resolve_chain(kind, settings, requested)

    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
    )
    import dubforge.pipeline as pipeline_module

    original = pipeline_module.registry
    pipeline_module.registry = Shim()
    try:
        with pytest.raises(pipeline.DubError) as exc:
            pipeline_module.dub(demo_clip, settings, output=tmp_path / "never.mp4")
    finally:
        pipeline_module.registry = original
    assert "no line could be synthesised" in str(exc.value)
    assert "empty audio file" in str(exc.value) or "nothing to place" in str(exc.value)


@needs_ffmpeg
def test_a_cache_failure_does_not_fail_the_dub(demo_clip, tmp_path, monkeypatch):
    """Losing the cache costs a future transcription, never this one."""
    import dubforge.pipeline as pipeline_module

    def broken_cache(*args, **kwargs):
        raise pipeline_module.CacheWriteError("another process holds the lock")

    monkeypatch.setattr(pipeline_module, "_save_cached", broken_cache)
    settings = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=True,
    )
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "cached.mp4")
    assert Path(result.output).exists()
    assert any("cache not written" in warning for warning in result.warnings)
    checks = {c["check"]: c for c in result.qc["checks"]}
    assert checks["every_line_has_audio"]["ok"] is True


# --------------------------------------------------------------- 0.2.0 behaviours


BASE_SETTINGS = {
    "source_lang": "en",
    "target_lang": "hi",
    "asr": "stub",
    "translator": "stub",
    "tts": "stub",
    "separator": "none",
    "cache": False,
}


@needs_ffmpeg
def test_a_cancelled_run_stops_before_the_next_stage(demo_clip, tmp_path):
    from dubforge.pipeline import DubCancelled

    settings = Settings(script=demo.DEMO_SCRIPT, workdir=str(tmp_path / "work"), **BASE_SETTINGS)
    with pytest.raises(DubCancelled):
        pipeline.dub(demo_clip, settings, output=tmp_path / "never.mp4", cancel=lambda: True)
    assert not (tmp_path / "never.mp4").exists()


@needs_ffmpeg
def test_the_providers_that_actually_ran_are_recorded(demo_clip, tmp_path):
    settings = Settings(script=demo.DEMO_SCRIPT, workdir=str(tmp_path / "work"), **BASE_SETTINGS)
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "p.mp4")
    assert result.providers_used["asr"] == "stub"
    assert result.providers_used["tts"] == "stub"
    assert result.providers_used["translate"] == "stub"
    manifest = json.loads((Path(result.workdir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["providers_used"] == result.providers_used


@needs_ffmpeg
def test_progress_is_reported_with_a_growing_step_count(demo_clip, tmp_path):
    seen: list[tuple[str, int, int]] = []
    settings = Settings(script=demo.DEMO_SCRIPT, workdir=str(tmp_path / "work"), **BASE_SETTINGS)
    pipeline.dub(
        demo_clip,
        settings,
        output=tmp_path / "prog.mp4",
        progress=lambda stage, done, total: seen.append((stage, done, total)),
    )
    assert len(seen) >= 5
    assert seen[-1][1] >= seen[0][1]
    assert all(total >= done for _, done, total in seen)


@needs_ffmpeg
def test_fallback_is_opt_in_and_is_announced(demo_clip, tmp_path, monkeypatch):
    """Switching providers changes quality and cost, so it must never be silent."""
    from dubforge.providers import registry as real_registry
    from dubforge.providers.base import ProviderError, Spec, TTSProvider

    class BrokenTTS(TTSProvider):
        spec = Spec(
            name="broken",
            kind="tts",
            title="Always fails",
            verified=False,
            quality="fails on purpose",
        )
        name = "broken"

        def voices(self, language=""):
            return []

        def synthesize(self, text, voice, out_path, style=None, emotion=None, sample_rate=48000):
            raise ProviderError("provider is down", kind="server", retryable=True)

    original_chain = real_registry.resolve_chain

    def chain(kind, settings=None, requested=None):
        if kind == "tts":
            return [BrokenTTS(), real_registry.create("tts", "stub", settings)]
        return original_chain(kind, settings, requested)

    monkeypatch.setattr(real_registry, "resolve_chain", chain)
    base = {**BASE_SETTINGS, "script": demo.DEMO_SCRIPT, "workdir": str(tmp_path / "work")}

    # fallback disabled: the failure is the failure
    with pytest.raises(pipeline.DubError):
        pipeline.dub(demo_clip, Settings(**base), output=tmp_path / "no-fallback.mp4")

    # fallback enabled: it completes, says so, and records who ran
    settings = Settings(**base).with_overrides(fallback=True)
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "fallback.mp4")
    assert Path(result.output).exists()
    assert result.providers_used["tts"] == "stub"
    assert any("falling back" in warning for warning in result.warnings), result.warnings


@needs_ffmpeg
def test_plan_refuses_a_file_over_the_duration_limit(demo_clip, tmp_path):
    settings = Settings(
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        max_input_seconds=2.0,
        **BASE_SETTINGS,
    )
    with pytest.raises(pipeline.DubError) as exc:
        pipeline.plan(demo_clip, settings)
    assert "limit" in str(exc.value).lower() or "max_input_seconds" in str(exc.value)


# ------------------------------------------------------------- QC additions (0.2.1)


@needs_ffmpeg
def test_qc_reports_subtitle_and_stream_health(demo_clip, tmp_path):
    settings = Settings(script=demo.DEMO_SCRIPT, workdir=str(tmp_path / "work"), **BASE_SETTINGS)
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "qc.mp4")
    checks = {item["check"]: item for item in result.qc["checks"]}
    for name in (
        "subtitle_timing",
        "subtitle_readability",
        "streams_present",
        "video_codec_preserved",
        "audio_layout",
        "not_mostly_silent",
    ):
        assert name in checks, f"{name} missing from QC"
    # the demo muxes the picture through unchanged and has a real dub track
    assert checks["streams_present"]["ok"] is True
    assert checks["video_codec_preserved"]["ok"] is True
    assert checks["subtitle_timing"]["ok"] is True


@needs_ffmpeg
def test_readability_thresholds_come_from_settings(demo_clip, tmp_path):
    """A one-character budget makes every line unreadable; that must be visible."""
    settings = Settings(
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        qc_max_chars_per_line=10,
        **BASE_SETTINGS,
    )
    result = pipeline.dub(demo_clip, settings, output=tmp_path / "tight.mp4")
    checks = {item["check"]: item for item in result.qc["checks"]}
    assert checks["subtitle_readability"]["ok"] is False
    assert checks["subtitle_readability"]["severity"] == "warn"


@needs_ffmpeg
def test_retries_are_counted_and_reported(demo_clip, tmp_path):
    """The retry sink must reach the result, so a job can say 'this run retried'."""
    from dubforge.providers import http as provider_http

    settings = Settings(script=demo.DEMO_SCRIPT, workdir=str(tmp_path / "work"), **BASE_SETTINGS)
    original = pipeline._pick_voices

    def note_a_retry(*args, **kwargs):
        provider_http._note_retry("stub", "HTTP 429 (rate_limit)", 0)
        return original(*args, **kwargs)

    import dubforge.pipeline as pipeline_module

    pipeline_module._pick_voices = note_a_retry
    try:
        result = pipeline.dub(demo_clip, settings, output=tmp_path / "retried.mp4")
    finally:
        pipeline_module._pick_voices = original
    assert result.retries
    assert any("provider retry" in warning for warning in result.warnings)
    report = Path(result.report).read_text(encoding="utf-8")
    assert "Provider retries" in report
