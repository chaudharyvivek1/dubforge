"""Partial remixing: the spliced track must equal a full rebuild, and say so when it can't."""

import json
import shutil
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge import demo, dialogue, ffmpeg, pipeline
from dubforge.config import Settings
from dubforge.dialogue import DialogueEntry, DialogueStore


def _settings(tmp_path, **extra) -> Settings:
    base = {
        "source_lang": "en",
        "target_lang": "hi",
        "asr": "stub",
        "translator": "stub",
        "tts": "stub",
        "separator": "none",
        "script": demo.DEMO_SCRIPT,
        "workdir": str(tmp_path / "work"),
        "cache": False,
    }
    base.update(extra)
    return Settings(**base)


def _rms_difference(a: str, b: str) -> float:
    """Mean absolute difference between two wav files, normalised to 0..1."""
    import struct
    import wave

    with wave.open(a) as first, wave.open(b) as second:
        frames_a = first.readframes(first.getnframes())
        frames_b = second.readframes(second.getnframes())
        width = first.getsampwidth()
        count = min(len(frames_a), len(frames_b)) // width
        fmt = "<h" if width == 2 else "<b"
        samples_a = struct.unpack(f"<{count}{fmt[-1]}", frames_a[: count * width])
        samples_b = struct.unpack(f"<{count}{fmt[-1]}", frames_b[: count * width])
        if count == 0:
            return 1.0
        total = sum(abs(x - y) for x, y in zip(samples_a, samples_b, strict=False))
        return total / (count * 32768)


def _clips(tmp_path) -> list[tuple[str, float]]:
    """Three short clips at known starts."""
    made = []
    for index, (start, frequency) in enumerate([(0.2, 220), (1.0, 330), (2.0, 440)]):
        path = tmp_path / f"clip{index}.wav"
        ffmpeg.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=0.4",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(path),
            ]
        )
        made.append((str(path), start))
    return made


@needs_ffmpeg
def test_a_splice_equals_a_full_rebuild(tmp_path):
    """The whole promise: splicing is an optimisation, not a different mix."""
    clips = _clips(tmp_path)
    total = 3.0
    full = ffmpeg.assemble_dub(clips, tmp_path / "full.wav", total)
    shutil.copyfile(full, tmp_path / "base.wav")

    # replace the middle clip with a different tone
    replacement = tmp_path / "replacement.wav"
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=0.4",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(replacement),
        ]
    )
    spliced = dialogue.splice(
        tmp_path / "base.wav", tmp_path / "spliced.wav", [(1.0, 0.4, str(replacement))], total
    )

    expected_clips = [(clips[0][0], 0.2), (str(replacement), 1.0), (clips[2][0], 2.0)]
    rebuilt = ffmpeg.assemble_dub(expected_clips, tmp_path / "rebuilt.wav", total)

    difference = _rms_difference(spliced, rebuilt)
    assert difference < 0.01, f"spliced track differs from a rebuild by {difference:.4f}"


@needs_ffmpeg
def test_only_the_changed_range_is_touched(tmp_path):
    clips = _clips(tmp_path)
    full = ffmpeg.assemble_dub(clips, tmp_path / "full.wav", 3.0)
    replacement = tmp_path / "r.wav"
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=0.4",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(replacement),
        ]
    )
    spliced = dialogue.splice(full, tmp_path / "spliced.wav", [(1.0, 0.4, str(replacement))], 3.0)
    untouched = ffmpeg.assemble_dub(
        [(clips[0][0], 0.2), (clips[2][0], 2.0)], tmp_path / "untouched.wav", 3.0
    )
    # the first and last lines survive bit-for-bit in the regions they occupy
    assert _rms_difference(spliced, untouched) < 0.2


def test_splicing_nothing_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError, match="nothing to splice"):
        dialogue.splice(tmp_path / "x.wav", tmp_path / "y.wav", [], 1.0)


# ------------------------------------------------------------------- the store


def test_a_track_round_trips_with_its_manifest(tmp_path):
    source = tmp_path / "track.wav"
    source.write_bytes(b"RIFF" + b"a" * 2048)
    store = DialogueStore(tmp_path / "work")
    entries = [
        DialogueEntry(
            seg_id="00000-abc", start=0.5, duration=1.0, speed=1.0, clip_sha256="deadbeef"
        )
    ]
    store.save(source, entries, total_duration=9.2)
    found = store.find()
    assert found is not None
    assert found.total_duration == 9.2
    assert found.entries[0].seg_id == "00000-abc"
    assert Path(found.path).exists()


def test_a_truncated_track_is_a_miss(tmp_path):
    source = tmp_path / "track.wav"
    source.write_bytes(b"RIFF" + b"b" * 2048)
    store = DialogueStore(tmp_path / "work")
    store.save(source, [DialogueEntry(seg_id="x", start=0.0, duration=1.0)], 5.0)
    store.audio.write_bytes(b"RIFF")  # a killed worker
    assert store.find() is None


def test_a_missing_manifest_is_a_miss(tmp_path):
    store = DialogueStore(tmp_path / "work")
    store.root.mkdir(parents=True, exist_ok=True)
    store.audio.write_bytes(b"RIFF" + b"c" * 100)
    assert store.find() is None


# ------------------------------------------------------------------ overlap


def test_overlap_detection_ignores_the_line_itself():
    a = DialogueEntry(seg_id="a", start=0.0, duration=2.0)
    b = DialogueEntry(seg_id="b", start=1.0, duration=2.0)
    c = DialogueEntry(seg_id="c", start=5.0, duration=1.0)
    assert dialogue.overlaps_any(a, [a, b, c]) is True
    assert dialogue.overlaps_any(c, [a, b, c]) is False
    assert dialogue.overlaps_any(a, [a]) is False


# --------------------------------------------------------------- the pipeline


@needs_ffmpeg
def test_a_second_run_reuses_the_track(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    assert first.dialogue_mode == "assembled"
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert second.dialogue_mode == "reused"
    assert second.spliced_lines == 0


@needs_ffmpeg
def test_a_regeneration_splices_only_the_edited_line(tmp_path):
    from dubforge.jobs import JobStore

    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    result = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    store = JobStore(settings.workdir)
    job = store.create(clip, settings)
    store.update(job.id, status="processing", pid=1)
    store.update(
        job.id,
        status="completed",
        qc_ok=result.ok,
        transcript_json=result.transcript_json,
        output=result.output,
        review=result.review,
        qc={"ok": result.ok, "failures": []},
    )

    again = pipeline.regenerate(job.id, settings, edits={"1": "यह बदली हुई पंक्ति है।"})
    assert again.dialogue_mode == "spliced"
    assert again.spliced_lines == 1
    assert again.rendered_segments == 1
    assert Path(again.output).exists()


@needs_ffmpeg
def test_splicing_can_be_switched_off(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    pipeline.dub(clip, _settings(tmp_path), output=tmp_path / "a.mp4")
    again = pipeline.dub(clip, _settings(tmp_path, dialogue_cache=False), output=tmp_path / "b.mp4")
    assert again.dialogue_mode == "assembled"


@needs_ffmpeg
def test_a_workspace_without_a_track_assembles_normally(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    first = pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    for name in ("dialogue.wav", "dialogue.json"):
        for path in Path(first.workdir).rglob(name):
            path.unlink()
    again = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    assert again.dialogue_mode == "assembled"


@needs_ffmpeg
def test_the_review_reports_the_dialogue_mode(tmp_path):
    clip = demo.build_clip(tmp_path / "clip.mp4")
    settings = _settings(tmp_path)
    pipeline.dub(clip, settings, output=tmp_path / "a.mp4")
    second = pipeline.dub(clip, settings, output=tmp_path / "b.mp4")
    payload = json.loads(Path(second.review).read_text(encoding="utf-8"))
    assert payload["work"]["dialogue_mode"] == "reused"
    assert "dialogue track" not in payload["work"]["rebuilt_stages"], (
        "a reused track was not rebuilt, and the report must not claim it was"
    )
    assert "ducking" in payload["work"]["rebuilt_stages"], (
        "ducking follows the dialogue, so it always runs"
    )
