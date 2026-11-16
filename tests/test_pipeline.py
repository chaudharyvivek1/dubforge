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
    assert not any("cached" in step.detail for step in other.steps)


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
        spec = type("S", (), {"name": "broken", "kind": "tts", "verified": False})()

        def voices(self, language=""):
            return []

        def synthesize(self, text, voice, out_path, style=None, emotion=None, sample_rate=48000):
            Path(out_path).write_bytes(b"")  # a 200 with nothing in it
            return SynthResult(path=str(out_path), duration=0.0, provider="broken")

    class Shim:
        def __getattr__(self, item):
            return getattr(real_registry, item)

        def resolve(self, kind, settings=None, requested=None):
            if kind == "tts":
                return BrokenTTS()
            return real_registry.resolve(kind, settings, requested)

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
