"""Retry safety and batch output collisions.

Two ways this project can cost a user money or destroy work, both fixed in 0.2.1:

* retrying a non-idempotent POST after a timeout — the vendor may have accepted and billed
  it, so a retry can buy the same line twice;
* two folders with the same filename writing to one output path — the second dub silently
  replaces the first.
"""

import json
import time
from pathlib import Path

import pytest

from dubforge import batch
from dubforge.config import Settings
from dubforge.jobs import JobStore
from dubforge.providers import http as http_module
from dubforge.providers.base import ProviderError

# --------------------------------------------------------------------- retries


class _Response:
    def __init__(self, status_code: int, body: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = body
        self.content = body.encode()
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text or "{}")


@pytest.fixture()
def fake_requests(monkeypatch):
    """Replace `requests.post` with a scripted responder."""
    script: list = []
    calls: list = []

    class FakeRequests:
        @staticmethod
        def post(url, headers=None, json=None, data=None, files=None, timeout=None):
            calls.append({"url": url, "headers": headers or {}})
            outcome = script.pop(0) if script else _Response(200, "{}")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(http_module, "_requests", lambda: FakeRequests)
    return {"script": script, "calls": calls}


def test_a_429_is_retried_and_retry_after_is_honoured(fake_requests, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: slept.append(seconds))
    fake_requests["script"].extend(
        [
            _Response(429, "slow down", {"Retry-After": "2"}),
            _Response(200, "{}"),
        ]
    )
    sink: list = []
    http_module.watch_retries(sink)
    response = http_module.post("https://api.test/v1", provider="test", retries=2)
    assert response.status_code == 200
    assert 2 in slept, f"Retry-After was ignored: {slept}"
    assert sink and sink[0]["reason"].startswith("HTTP 429")


def test_a_401_is_not_retried(fake_requests):
    fake_requests["script"].extend([_Response(401, "bad key"), _Response(200, "{}")])
    with pytest.raises(ProviderError) as exc:
        http_module.post("https://api.test/v1", provider="test", retries=3)
    assert exc.value.kind == "auth"
    assert exc.value.retryable is False
    assert len(fake_requests["calls"]) == 1, "an auth failure must not be retried"


def test_a_5xx_is_retried_because_the_vendor_rejected_it(fake_requests, monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    fake_requests["script"].extend([_Response(503, "down"), _Response(200, "{}")])
    assert http_module.post("https://api.test/v1", provider="test", retries=2).status_code == 200
    assert len(fake_requests["calls"]) == 2


def test_a_timeout_on_a_non_idempotent_call_is_not_retried(fake_requests):
    """The expensive case: the vendor may have accepted and billed the request."""
    fake_requests["script"].extend([TimeoutError("connection timed out"), _Response(200, "{}")])
    with pytest.raises(ProviderError) as exc:
        http_module.post("https://api.test/v1/tts", provider="tts", retries=3)
    assert exc.value.kind == "ambiguous"
    assert exc.value.retryable is False
    assert "may have been accepted and billed" in str(exc.value)
    assert len(fake_requests["calls"]) == 1, "a non-idempotent call was retried after a timeout"


def test_a_timeout_on_an_idempotent_call_is_retried(fake_requests, monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    fake_requests["script"].extend([TimeoutError("boom"), _Response(200, "{}")])
    response = http_module.post(
        "https://api.test/v1/models", provider="test", retries=2, idempotent=True
    )
    assert response.status_code == 200
    assert len(fake_requests["calls"]) == 2


def test_an_idempotency_key_makes_a_retry_safe_and_is_sent(fake_requests, monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    fake_requests["script"].extend([TimeoutError("boom"), _Response(200, "{}")])
    http_module.post(
        "https://api.openai.com/v1/audio/speech",
        provider="openai",
        retries=2,
        idempotency_key="abc123",
    )
    assert len(fake_requests["calls"]) == 2
    assert fake_requests["calls"][0]["headers"].get("Idempotency-Key") == "abc123"


def test_the_retry_sink_records_no_secrets(fake_requests, monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    fake_requests["script"].extend([_Response(500, "boom"), _Response(200, "{}")])
    sink: list = []
    http_module.watch_retries(sink)
    http_module.post(
        "https://api.test/v1", headers={"Authorization": "Bearer sk-secret"}, provider="test"
    )
    blob = json.dumps(sink)
    assert "sk-secret" not in blob
    assert sink[0]["provider"] == "test"


def test_retries_are_bounded_by_the_budget(fake_requests, monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)
    fake_requests["script"].extend([_Response(503, "down")] * 5)
    with pytest.raises(ProviderError) as exc:
        http_module.post("https://api.test/v1", provider="test", retries=1)
    assert exc.value.kind == "server"
    assert len(fake_requests["calls"]) == 2  # one attempt plus one retry


# ---------------------------------------------------------------- batch outputs


def _settings(tmp_path, **extra) -> Settings:
    return Settings(target_lang="hi", workdir=str(tmp_path / "work"), **extra)


def test_same_basename_in_different_folders_gets_distinct_outputs(tmp_path):
    root = tmp_path / "lessons"
    (root / "class-a").mkdir(parents=True)
    (root / "class-b").mkdir(parents=True)
    a = root / "class-a" / "lesson.mp4"
    b = root / "class-b" / "lesson.mp4"
    for path in (a, b):
        path.write_bytes(b"x")

    settings = _settings(tmp_path)
    out_a = batch.output_for(a, settings, out_dir=tmp_path / "out", batch_root=root)
    out_b = batch.output_for(b, settings, out_dir=tmp_path / "out", batch_root=root)
    assert out_a != out_b
    assert out_a.parent.name == "class-a" and out_b.parent.name == "class-b"


def test_an_existing_output_is_never_replaced(tmp_path):
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"x")
    settings = _settings(tmp_path)
    first = batch.output_for(source, settings, out_dir=tmp_path / "out")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"previous dub")
    second = batch.output_for(source, settings, out_dir=tmp_path / "out")
    assert second != first
    assert first.read_bytes() == b"previous dub"
    # deterministic: asking again gives the same alternative name, not a new one
    assert batch.output_for(source, settings, out_dir=tmp_path / "out") == second


def test_an_output_is_never_the_source(tmp_path):
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"x")
    settings = _settings(tmp_path)
    target = batch.output_for(source, settings, out_dir=tmp_path)
    assert target.resolve() != source.resolve()


def test_the_language_tag_is_added_once(tmp_path):
    settings = _settings(tmp_path)
    tagged = tmp_path / "lesson.hi.mp4"
    tagged.write_bytes(b"x")
    assert batch.output_for(tagged, settings, out_dir=tmp_path / "out").name == "lesson.hi.mp4"


def test_audio_sources_become_m4a(tmp_path):
    settings = _settings(tmp_path)
    assert (
        batch.output_for(tmp_path / "talk.mp3", settings, out_dir=tmp_path / "out").suffix == ".m4a"
    )


def test_collect_sources_deduplicates_a_symlink(tmp_path):
    folder = tmp_path / "lessons"
    folder.mkdir()
    real = folder / "a.mp4"
    real.write_bytes(b"x")
    try:
        (folder / "link.mp4").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    found = batch.collect_sources(folder)
    assert len(found) == 1, [p.name for p in found]


def test_collect_sources_skips_outputs_without_a_200_job_limit(tmp_path):
    """Output discovery must not stop working after 200 historical jobs."""
    settings = _settings(tmp_path)
    store = JobStore(settings.workdir)
    folder = tmp_path / "lessons"
    folder.mkdir()
    output = folder / "a.hi.mp4"
    output.write_bytes(b"dub")
    (folder / "a.mp4").write_bytes(b"x")

    for index in range(250):
        fake = store.registry / f"bulk{index}-00000000.json"
        fake.write_text(
            json.dumps(
                {
                    "id": f"bulk{index}-00000000",
                    "source": f"/x/{index}.mp4",
                    "target_lang": "hi",
                    "status": "completed",
                    "output": str(output) if index == 249 else "",
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )

    found = batch.collect_sources(folder, store=store)
    assert [p.name for p in found] == ["a.mp4"]


def test_output_paths_is_cheap_and_complete(tmp_path):
    settings = _settings(tmp_path)
    store = JobStore(settings.workdir)
    for index in range(50):
        (store.registry / f"j{index}-00000000.json").write_text(
            json.dumps(
                {
                    "id": f"j{index}-00000000",
                    "source": "/x.mp4",
                    "target_lang": "hi",
                    "status": "completed",
                    "output": f"/out/{index}.mp4",
                }
            ),
            encoding="utf-8",
        )
    paths = store.output_paths()
    assert len(paths) == 50


def test_a_batch_never_overwrites_a_previous_result(tmp_path, monkeypatch):
    """The second run of the same batch skips a file that already passed QC."""
    from dubforge import demo

    monkeypatch.setenv("DUBFORGE_SCRIPT", demo.DEMO_SCRIPT)
    folder = tmp_path / "lessons"
    folder.mkdir()
    demo.build_clip(folder / "a.mp4")
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
    sources = batch.collect_sources(folder, store=JobStore(settings.workdir))
    first = batch.run_batch(sources, settings)
    assert first.counts["total"] == 1
    output = Path(first.items[0].output)
    assert output.exists()
    stamp = output.stat().st_mtime_ns

    # pretend that run passed QC, then run the batch again
    store = JobStore(settings.workdir)
    job = store.get(first.items[0].job_id)
    store.update(job.id, qc_ok=True)
    second = batch.run_batch(batch.collect_sources(folder, store=store), settings)
    assert second.counts["skipped"] == 1
    assert output.stat().st_mtime_ns == stamp
