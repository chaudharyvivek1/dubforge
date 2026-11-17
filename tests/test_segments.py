"""Segment-level regeneration: reuse what did not change, re-render what did.

The promise being tested: editing one translated line costs one synthesis, not a film's
worth; and nothing about that convenience may damage a previous result.
"""

import json
import threading
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, pipeline
from dubforge.config import Settings
from dubforge.jobs import JobStore
from dubforge.media import Segment, Transcript
from dubforge.segments import (
    RenderRecord,
    SegmentStore,
    fingerprint,
    restore,
    segment_id,
    sha256_file,
)


def make_segment(index=0, start=0.0, end=2.0, text="hello", voice="v1") -> Segment:
    segment = Segment(index=index, start=start, end=end, text=text, voice=voice)
    segment.target_text = text
    return segment


def _fp(segment, provider="stub", model="", language="hi") -> str:
    return fingerprint(
        segment, provider=provider, model=model, language=language, sample_rate=48000
    )


# ------------------------------------------------------------------- the store


def test_segment_ids_are_stable_and_distinct(tmp_path):
    a = make_segment(index=0, start=0.0)
    b = make_segment(index=1, start=2.0)
    assert segment_id(a) == segment_id(make_segment(index=0, start=0.0))
    assert segment_id(a) != segment_id(b)
    assert segment_id(a).startswith("00000-")


def test_the_fingerprint_covers_every_input_that_changes_the_audio():
    base = make_segment()
    original = _fp(base)

    for mutate in (
        lambda seg: setattr(seg, "target_text", "different"),
        lambda seg: setattr(seg, "voice", "v2"),
        lambda seg: setattr(seg, "style", "cheerful"),
    ):
        changed = make_segment()
        mutate(changed)
        assert _fp(changed) != original, mutate

    assert _fp(base, provider="elevenlabs") != original
    assert _fp(base, model="v3") != original
    assert _fp(base, language="ta") != original


def test_the_fingerprint_ignores_timing_that_only_affects_the_stretch():
    """The raw render is cached, so the window must not invalidate it."""
    early = make_segment(start=0.0, end=2.0)
    late = make_segment(start=5.0, end=7.0)
    assert _fp(early) == _fp(late)


def test_a_record_round_trips_and_is_found(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"x" * 200)
    segment = make_segment()
    record = RenderRecord(
        seg_id=segment_id(segment),
        fingerprint=_fp(segment),
        audio="",
        audio_sha256="",
        duration_s=1.8,
        provider="stub",
        voice="v1",
        language="hi",
        target_text="hello",
    )
    store.save(record, audio)

    found = store.find(record.seg_id, _fp(segment))
    assert found is not None
    assert found.duration_s == 1.8
    assert Path(found.audio).exists()
    assert found.audio_sha256 == sha256_file(found.audio)


def test_a_changed_fingerprint_is_a_miss(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"x" * 200)
    segment = make_segment()
    store.save(
        RenderRecord(
            seg_id=segment_id(segment), fingerprint="old", audio="", audio_sha256="", duration_s=1.0
        ),
        audio,
    )
    assert store.find(segment_id(segment), "new") is None


def test_a_truncated_audio_file_is_a_miss_not_a_silent_hole(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"x" * 400)
    segment = make_segment()
    record = store.save(
        RenderRecord(
            seg_id=segment_id(segment), fingerprint="fp", audio="", audio_sha256="", duration_s=1.0
        ),
        audio,
    )
    Path(record.audio).write_bytes(b"")  # a killed worker, or a bad disk
    assert store.find(segment_id(segment), "fp") is None


def test_an_absent_or_corrupt_record_is_a_miss(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    assert store.find("00000-aaaaaaaa", "fp") is None
    store.dir()
    store.meta_path("00000-bbbbbbbb").write_text("{not json", encoding="utf-8")
    assert store.find("00000-bbbbbbbb", "fp") is None


def test_one_worker_at_a_time_per_segment(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    with store.claim("seg"), store.claim("seg") as second:
        assert second is False, "two workers must not write the same segment"
    with store.claim("seg") as third:
        assert third is True, "the lock must be released"


def test_a_stale_segment_lock_is_reclaimed(tmp_path):
    import os
    import time

    store = SegmentStore(tmp_path / "segments")
    lock = store.dir() / "seg.lock"
    lock.write_text("{}", encoding="utf-8")
    old = time.time() - 600
    os.utime(lock, (old, old))
    with store.claim("seg") as acquired:
        assert acquired is True


def test_saving_is_atomic_and_leaves_no_temporary_files(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"y" * 100)
    store.save(
        RenderRecord(
            seg_id="00000-cccccccc", fingerprint="fp", audio="", audio_sha256="", duration_s=1.0
        ),
        audio,
    )
    leftovers = [p.name for p in store.dir().iterdir() if p.suffix in (".tmp", ".lock")]
    assert leftovers == []


def test_restore_copies_the_render_where_the_pipeline_expects_it(tmp_path):
    store = SegmentStore(tmp_path / "segments")
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF" + b"z" * 50)
    record = store.save(
        RenderRecord(
            seg_id="00000-dddddddd", fingerprint="fp", audio="", audio_sha256="", duration_s=1.0
        ),
        audio,
    )
    destination = tmp_path / "tts" / "00000.wav"
    assert restore(record, destination) == str(destination)
    assert destination.read_bytes().startswith(b"RIFF")


# ------------------------------------------------------- pipeline integration


@pytest.fixture()
def clip(tmp_path):
    return demo.build_clip(tmp_path / "clip.mp4")


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        script=demo.DEMO_SCRIPT,
        workdir=str(tmp_path / "work"),
        cache=False,
        **extra,
    )


@needs_ffmpeg
def test_a_second_run_reuses_every_unchanged_render(clip, tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    assert first.rendered_segments == 3
    assert first.reused_segments == 0

    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.rendered_segments == 0
    assert second.reused_segments == 3
    assert Path(second.output).exists()


@needs_ffmpeg
def test_reuse_can_be_turned_off(clip, tmp_path):
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    again = pipeline.dub(clip, _settings(tmp_path, segment_reuse=False), output=tmp_path / "b.mp4")
    assert again.rendered_segments == 3


@needs_ffmpeg
def test_a_changed_voice_invalidates_only_that_speaker(clip, tmp_path):
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")

    changed = _settings(tmp_path, voice_map={"SPEAKER_00": "different-voice"})
    result = pipeline.dub(clip, changed, output=tmp_path / "b.mp4")
    # The demo has two speakers; only one voice changed.
    assert result.rendered_segments >= 1
    assert result.reused_segments >= 1
    assert result.rendered_segments + result.reused_segments == 3


@needs_ffmpeg
def test_a_changed_provider_invalidates_everything(clip, tmp_path):
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    result = pipeline.dub(
        clip, _settings(tmp_path, tts_model="other-model"), output=tmp_path / "b.mp4"
    )
    assert result.rendered_segments == 3
    assert result.reused_segments == 0


@needs_ffmpeg
def test_a_corrupt_cached_render_is_regenerated(clip, tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    store = SegmentStore(Path(first.workdir) / "segments")
    victims = sorted(store.dir().glob("*.wav"))
    assert victims
    victims[0].write_bytes(b"")

    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.rendered_segments == 1, "only the damaged line should be re-rendered"
    assert second.reused_segments == 2


@needs_ffmpeg
def test_an_old_workspace_without_segments_still_runs(clip, tmp_path):
    """Backward compatibility: no segment store means a full render, not a failure."""
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    import shutil

    shutil.rmtree(Path(first.workdir) / "segments")
    again = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert again.rendered_segments == 3
    assert Path(again.output).exists()


@needs_ffmpeg
def test_concurrent_runs_do_not_corrupt_the_segment_store(clip, tmp_path):
    """Two runs of the same job: the lock serialises the write, neither fails."""
    settings = _settings(tmp_path)
    errors: list[Exception] = []

    def run(name: str) -> None:
        try:
            pipeline.dub(clip, settings, output=tmp_path / f"{name}.mp4")
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{index}",)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    store = SegmentStore(next(tmp_path.glob("work/jobs/*/segments")))
    for record_path in store.dir().glob("*.json"):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["audio_sha256"], "every stored record must carry a checksum"
        assert Path(store.dir() / record["audio"]).exists()


# --------------------------------------------------------------- regeneration


@needs_ffmpeg
def _completed_job(clip, settings, tmp_path) -> tuple[JobStore, str, object]:
    result = pipeline.dub(clip, settings, output=tmp_path / "first.mp4")
    store = JobStore(settings.workdir)
    job = store.create(clip, settings)
    store.update(job.id, status="processing", pid=1)
    store.update(
        job.id,
        status="completed",
        qc_ok=result.ok,
        transcript_json=result.transcript_json,
        output=result.output,
        audio=result.audio,
        subtitles=result.subtitles,
        report=result.report,
        qc={"ok": result.ok, "failures": result.qc["failures"]},
    )
    return store, job.id, result


@needs_ffmpeg
def test_regenerating_one_line_reuses_the_others(clip, tmp_path):
    settings = _settings(tmp_path)
    store, job_id, first = _completed_job(clip, settings, tmp_path)

    result = pipeline.regenerate(job_id, settings, edits={"0": "यह एक नई पंक्ति है।"})
    assert result.rendered_segments == 1
    assert result.reused_segments == 2
    assert Path(result.output).exists()

    segments = Transcript.load(result.transcript_json).segments
    assert segments[0].speech == "यह एक नई पंक्ति है।"
    assert Path(first.output).exists(), "the previous output must survive"
    assert Path(result.output) != Path(first.output)


@needs_ffmpeg
def test_regeneration_never_overwrites_the_previous_output(clip, tmp_path):
    settings = _settings(tmp_path)
    store, job_id, first = _completed_job(clip, settings, tmp_path)
    before = Path(first.output).read_bytes()

    pipeline.regenerate(job_id, settings, edits={"0": "पहला संपादन।"})
    assert Path(first.output).read_bytes() == before


@needs_ffmpeg
def test_regeneration_keeps_the_edits_rather_than_retranslating(clip, tmp_path):
    settings = _settings(tmp_path)
    store, job_id, _ = _completed_job(clip, settings, tmp_path)
    result = pipeline.regenerate(job_id, settings, edits={"1": "दूसरी पंक्ति संपादित।"})
    segments = Transcript.load(result.transcript_json).segments
    assert segments[1].speech == "दूसरी पंक्ति संपादित।"
    assert any(step.name == "translate" and "regeneration" in step.detail for step in result.steps)
    assert any("no translator was called" in warning for warning in result.warnings)


@needs_ffmpeg
def test_an_unknown_segment_id_is_refused(clip, tmp_path):
    settings = _settings(tmp_path)
    store, job_id, _ = _completed_job(clip, settings, tmp_path)
    with pytest.raises(pipeline.DubError, match="no segment"):
        pipeline.regenerate(job_id, settings, edits={"999": "nope"})


def test_a_job_without_a_transcript_cannot_be_regenerated(tmp_path):
    source = tmp_path / "x.mp4"
    source.write_bytes(b"x")
    settings = Settings(target_lang="hi", workdir=str(tmp_path / "work"))
    store = JobStore(settings.workdir)
    job = store.create(source, settings)
    with pytest.raises(pipeline.DubError, match="no transcript"):
        pipeline.regenerate(job.id, settings, edits={"0": "x"})


@needs_ffmpeg
def test_regeneration_reruns_qc(clip, tmp_path):
    settings = _settings(tmp_path)
    store, job_id, _ = _completed_job(clip, settings, tmp_path)
    result = pipeline.regenerate(job_id, settings, edits={"0": "संपादित।"})
    assert result.qc["checks"], "QC must run again on the rebuilt dub"
    assert len(result.qc["checks"]) >= 20


# ---------------------------------------------------- the documented --transcript


@needs_ffmpeg
def test_a_supplied_transcript_is_used_instead_of_recognition(clip, tmp_path):
    """`--transcript` was documented and ignored before 0.3.0."""
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")

    supplied = Settings(
        source_lang="en",
        target_lang="hi",
        asr="transcript",  # would fail: this provider needs a file
        translator="stub",
        tts="stub",
        separator="none",
        transcript=first.transcript_json,
        workdir=str(tmp_path / "work2"),
        cache=False,
    )
    result = pipeline.dub(clip, supplied, output=tmp_path / "b.mp4")
    assert any("supplied" in step.detail for step in result.steps if step.name == "transcribe")
    assert Path(result.output).exists()


@needs_ffmpeg
def test_a_supplied_srt_is_accepted(clip, tmp_path):
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    srt_path = next(Path(first.workdir).glob("*.source.srt"))
    supplied = Settings(
        source_lang="en",
        target_lang="hi",
        asr="stub",
        translator="stub",
        tts="stub",
        separator="none",
        transcript=str(srt_path),
        workdir=str(tmp_path / "work2"),
        cache=False,
    )
    result = pipeline.dub(clip, supplied, output=tmp_path / "b.mp4")
    assert Path(result.output).exists()


def test_a_missing_transcript_file_is_a_clear_error(tmp_path):
    with pytest.raises(pipeline.DubError, match="no transcript at"):
        pipeline._load_supplied_transcript(tmp_path / "absent.json")
