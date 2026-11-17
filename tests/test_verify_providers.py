"""The live-verification harness: safe without keys, honest about what it proved."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_providers  # noqa: E402

from dubforge.config import Settings  # noqa: E402


def test_a_missing_key_is_skipped_not_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    record = verify_providers.verify("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
    assert record["status"] == "skipped"
    assert "ELEVENLABS_API_KEY" in record["detail"]
    assert record["provider"] == "elevenlabs"
    assert record["checked_at"].endswith("Z")


def test_a_provider_error_is_recorded_with_its_kind(tmp_path, monkeypatch):
    from dubforge.providers import registry
    from dubforge.providers.base import ProviderError

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    class Boom:
        def missing(self):
            return []

        def voices(self, language=""):
            raise ProviderError("nope", kind="auth", status=401)

    monkeypatch.setattr(registry, "create", lambda kind, name, settings: Boom())
    record = verify_providers.verify("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
    assert record["status"] == "failed"
    assert record["error_kind"] == "auth"


def test_a_schema_surprise_is_a_failure_not_a_pass(tmp_path, monkeypatch):
    from dubforge.providers import registry

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    class Silent:
        def missing(self):
            return []

        def voices(self, language=""):
            return []

        def synthesize(self, text, voice, out_path, style=None, emotion=None, sample_rate=48000):
            Path(out_path).write_bytes(b"")
            from dubforge.providers.base import SynthResult

            return SynthResult(path=str(out_path), duration=0.0, provider="silent")

    monkeypatch.setattr(registry, "create", lambda kind, name, settings: Silent())
    record = verify_providers.verify("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
    assert record["status"] == "failed"
    assert record["error_kind"] == "schema"


def test_dry_run_calls_nothing(capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = verify_providers.main(["--dry-run", "--only", "tts", "--provider", "openai"])
    out = capsys.readouterr().out
    assert code == 0
    assert "not ready" in out
    assert "OPENAI_API_KEY" in out


def test_a_written_record_never_contains_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-print-me")
    monkeypatch.setattr(verify_providers, "RECORD", tmp_path / "verification.json")
    verify_providers.main(["--only", "tts", "--provider", "openai", "--write"])
    text = (tmp_path / "verification.json").read_text(encoding="utf-8")
    assert "sk-do-not-print-me" not in text
    payload = json.loads(text)
    assert payload["note"].startswith("Produced by scripts/verify_providers.py")
    assert payload["results"][0]["provider"] == "openai"


def test_the_committed_verification_file_is_valid_json():
    path = Path(__file__).resolve().parents[1] / "docs" / "verification.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "results" in payload
    assert payload["note"].startswith("Produced by scripts/verify_providers.py")
