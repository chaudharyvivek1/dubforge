"""Concurrency regressions for the MCP transcription path.

The bug these exist for: `transcribe_video` released the workspace lock in a `finally`
block *before* saving the transcript, so a second request could claim the same
directory and overwrite the first one's file — with no error anywhere, just the wrong
transcript on disk.
"""

import json
import threading
from pathlib import Path

import pytest
from conftest import needs_ffmpeg

from dubforge.media import Segment, Transcript
from dubforge.server import run_transcription
from dubforge.workspace import open_job


class FakeASR:
    """Stands in for a real recogniser: no network, no model, controllable failures."""

    def __init__(
        self,
        name: str = "fake",
        text: str = "hello",
        fail: Exception | None = None,
        delay: float = 0.0,
    ):
        self.name = name
        self.text = text
        self.fail = fail
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def transcribe(self, audio, language=None, diarize=True):
        with self._lock:
            self.calls += 1
        if self.delay:
            threading.Event().wait(self.delay)
        if self.fail:
            raise self.fail
        return Transcript(
            segments=[Segment(index=0, start=0.0, end=1.0, text=self.text)],
            language=language or "en",
            provider=self.name,
            speakers=["SPEAKER_00"],
        )


def _run(provider, candidate, workdir):
    return run_transcription(Path(candidate), "en", provider, True, workdir)


@needs_ffmpeg
def test_two_simultaneous_requests_do_not_share_a_directory(demo_clip, tmp_path):
    """Same source, two agents, at once: two directories, two intact transcripts."""
    work = tmp_path / "work"
    results: list[dict] = []
    errors: list[Exception] = []

    def worker(text: str) -> None:
        try:
            results.append(_run(FakeASR(text=text, delay=0.3), demo_clip, work))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(text,)) for text in ("first agent", "second agent")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(results) == 2
    paths = [Path(result["transcript_path"]) for result in results]
    assert paths[0] != paths[1], "two concurrent requests wrote to the same file"
    assert len({result["job"] for result in results}) == 2, "both requests used one job directory"

    texts = set()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        texts.add(data["segments"][0]["text"])
    assert texts == {"first agent", "second agent"}, "one request overwrote another's transcript"


@needs_ffmpeg
def test_same_filename_in_different_folders_stays_separate(demo_clip, tmp_path):
    """Two different videos called clip.mp4 must not collide on the way to disk."""
    first = tmp_path / "class9" / "clip.mp4"
    second = tmp_path / "class10" / "clip.mp4"
    for target in (first, second):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(demo_clip).read_bytes())

    work = tmp_path / "work"
    one = _run(FakeASR(text="class nine"), first, work)
    two = _run(FakeASR(text="class ten"), second, work)
    assert one["transcript_path"] != two["transcript_path"]
    assert (
        json.loads(Path(one["transcript_path"]).read_text())["segments"][0]["text"] == "class nine"
    )
    assert (
        json.loads(Path(two["transcript_path"]).read_text())["segments"][0]["text"] == "class ten"
    )


@needs_ffmpeg
def test_a_provider_failure_releases_the_lock_and_leaves_no_transcript(demo_clip, tmp_path):
    work = tmp_path / "work"
    with pytest.raises(RuntimeError):
        _run(FakeASR(fail=RuntimeError("provider exploded")), demo_clip, work)

    jobs = list((work / "jobs").iterdir())
    assert len(jobs) == 1
    assert not (jobs[0] / ".lock").exists(), "a failed transcription must release the lock"
    assert not (jobs[0] / "transcript.json").exists(), (
        "a failed transcription must not leave a file"
    )


@needs_ffmpeg
def test_a_save_failure_releases_the_lock_and_leaves_no_partial_file(
    demo_clip, tmp_path, monkeypatch
):
    work = tmp_path / "work"
    original = Transcript.save

    def broken_save(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(Transcript, "save", broken_save)
    try:
        with pytest.raises(OSError):
            _run(FakeASR(), demo_clip, work)
    finally:
        monkeypatch.setattr(Transcript, "save", original)

    job_dir = next((work / "jobs").iterdir())
    assert not (job_dir / ".lock").exists(), "a failed save must release the lock"
    assert not (job_dir / "transcript.json").exists()
    assert not list(job_dir.glob("*.tmp")), "an atomic write must not leave temp files behind"


@needs_ffmpeg
def test_a_second_request_after_the_first_finishes_reuses_the_directory_safely(demo_clip, tmp_path):
    """Sequential requests may share the directory — but the second must fully overwrite."""
    work = tmp_path / "work"
    first = _run(FakeASR(text="version one"), demo_clip, work)
    second = _run(FakeASR(text="version two"), demo_clip, work)
    assert first["transcript_path"] == second["transcript_path"]
    data = json.loads(Path(second["transcript_path"]).read_text(encoding="utf-8"))
    assert data["segments"][0]["text"] == "version two"
    assert not (Path(second["transcript_path"]).parent / ".lock").exists()


@needs_ffmpeg
def test_the_lock_is_actually_held_during_the_write(demo_clip, tmp_path):
    """A provider that inspects the directory mid-call must see the lock in place."""
    work = tmp_path / "work"
    seen: dict = {}

    class SpyASR(FakeASR):
        def transcribe(self, audio, language=None, diarize=True):
            job_dirs = list((work / "jobs").iterdir())
            seen["locks"] = [(d / ".lock").exists() for d in job_dirs]
            return super().transcribe(audio, language, diarize)

    _run(SpyASR(), demo_clip, work)
    assert seen["locks"] == [True], "the workspace lock was released before the write"


@needs_ffmpeg
def test_two_requests_with_no_content_check_still_get_separate_jobs(demo_clip, tmp_path):
    """Isolation must come from the lock, not from the manifest being fresh."""
    work = tmp_path / "work"
    held = open_job(work, demo_clip, fingerprint="held")
    try:
        other = open_job(work, demo_clip, fingerprint="held")
        try:
            assert other.path != held.path
        finally:
            other.release()
    finally:
        held.release()
