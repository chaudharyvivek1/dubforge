"""The verification ledger: safe without keys, strict about what it accepts as evidence.

The implementation moved from `scripts/verify_providers.py` into `dubforge.verify` in 0.8.0 so
it could be planned, imported and tested without a key. Every assertion below is the one the
script's tests made, plus the ledger rules that came with the move.
"""

import json
from pathlib import Path

import pytest

from dubforge import verify
from dubforge.config import Settings

# --------------------------------------------------------------- one probe


def test_a_missing_key_is_skipped_not_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    record = verify.verify_one("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
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
    record = verify.verify_one("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
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
    record = verify.verify_one("tts", "elevenlabs", Settings(target_lang="hi"), tmp_path)
    assert record["status"] == "failed"
    assert record["error_kind"] == "schema"


# ------------------------------------------------------------------- plan


def test_the_plan_lists_every_adapter_and_its_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = verify.plan(Settings())
    rows = {(row["stage"], row["provider"]): row for row in data["adapters"]}
    assert ("tts", "openai") in rows
    assert rows[("tts", "openai")]["env"] == ["OPENAI_API_KEY"]
    assert rows[("tts", "openai")]["ready"] is False
    assert data["total"] >= 10
    assert data["verified"] == 0, "nothing is verified in a fresh checkout"


def test_the_plan_marks_what_a_present_key_would_unlock(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-present-for-this-test-only")
    rows = {(row["stage"], row["provider"]): row for row in verify.plan(Settings())["adapters"]}
    assert rows[("tts", "openai")]["ready"] is True
    assert rows[("tts", "openai")]["env_missing"] == []


def test_the_plan_asks_about_readiness_but_calls_nothing(monkeypatch):
    """`plan` builds each adapter to ask `missing()`, and must not go further than that."""
    from dubforge.providers import registry

    class Spy:
        def __init__(self, provider):
            self._provider = provider
            self.spec = provider.spec

        def missing(self):
            return self._provider.missing()

        def __getattr__(self, name):
            if name in ("transcribe", "translate", "synthesize", "voices", "separate", "sync"):
                raise AssertionError(f"plan called {name}()")
            return getattr(self._provider, name)

    real_create = registry.create
    monkeypatch.setattr(
        registry, "create", lambda kind, name, settings: Spy(real_create(kind, name, settings))
    )
    data = verify.plan(Settings())
    assert data["adapters"], "the plan must still describe every adapter"


def test_the_plan_renders_a_table():
    text = verify.render_plan(verify.plan(Settings()))
    assert "stage" in text and "provider" in text and "verified" in text
    assert "adapters verified" in text


# -------------------------------------------------------------------- run


def test_running_without_an_opt_in_is_refused(tmp_path):
    with pytest.raises(verify.VerifyError, match="refusing"):
        verify.run(Settings(), yes_live=False, write=False, tmp=tmp_path)


def test_a_run_with_no_ready_adapters_records_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    payload = verify.run(
        Settings(target_lang="hi"),
        yes_live=True,
        only=["tts"],
        providers=["elevenlabs"],
        write=False,
        tmp=tmp_path,
    )
    assert payload["results"][0]["status"] == "skipped"
    assert payload["schema"] == verify.SCHEMA


def test_a_written_record_never_contains_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-print-me")
    payload = verify.run(
        Settings(target_lang="hi"),
        yes_live=True,
        only=["tts"],
        providers=["openai"],
        write=False,
        tmp=tmp_path,
    )
    text = json.dumps(payload)
    assert "sk-do-not-print-me" not in text


def test_saving_and_loading_round_trips(tmp_path):
    path = tmp_path / "verification.json"
    payload = {
        "schema": verify.SCHEMA,
        "generated_at": "2026-11-18T00:00:00Z",
        "results": [
            {
                "stage": "tts",
                "provider": "openai",
                "status": "ok",
                "checked_at": "2026-11-18T00:00:00Z",
            }
        ],
    }
    verify.save(payload, path)
    assert verify.load(path)["results"][0]["provider"] == "openai"


def test_the_committed_record_is_valid_json():
    payload = json.loads(verify.RECORD.read_text(encoding="utf-8"))
    assert "results" in payload
    assert payload["note"].startswith("Produced by")


# ----------------------------------------------------------------- import


def _record(rows, **extra) -> dict:
    payload = {"schema": verify.SCHEMA, "generated_at": "2026-11-18T00:00:00Z", "results": rows}
    payload.update(extra)
    return payload


def test_an_imported_record_with_a_real_row_is_accepted():
    verdict = verify.validate(
        _record(
            [
                {
                    "stage": "tts",
                    "provider": "openai",
                    "status": "ok",
                    "checked_at": "2026-11-18T00:00:00Z",
                    "detail": "audio rendered",
                },
            ]
        )
    )
    assert verdict["ok"] is True
    assert verdict["ok_rows"] == 1


def test_an_unknown_adapter_is_refused():
    verdict = verify.validate(
        _record(
            [
                {
                    "stage": "tts",
                    "provider": "not-a-provider",
                    "status": "ok",
                    "checked_at": "2026-11-18T00:00:00Z",
                },
            ]
        )
    )
    assert verdict["ok"] is False
    assert any("unknown adapter" in problem for problem in verdict["problems"])


def test_a_duplicate_row_is_refused():
    row = {
        "stage": "tts",
        "provider": "openai",
        "status": "ok",
        "checked_at": "2026-11-18T00:00:00Z",
    }
    verdict = verify.validate(_record([row, dict(row)]))
    assert verdict["ok"] is False
    assert any("appears twice" in problem for problem in verdict["problems"])


def test_an_unknown_status_is_refused():
    verdict = verify.validate(
        _record(
            [
                {"stage": "tts", "provider": "openai", "status": "probably-fine"},
            ]
        )
    )
    assert verdict["ok"] is False
    assert any("is not one of" in problem for problem in verdict["problems"])


def test_an_ok_row_without_a_date_is_refused():
    """A claim of verification without a date is a claim you cannot audit."""
    verdict = verify.validate(
        _record(
            [
                {"stage": "tts", "provider": "openai", "status": "ok", "checked_at": "soon"},
            ]
        )
    )
    assert verdict["ok"] is False
    assert any("ISO date" in problem for problem in verdict["problems"])


def test_a_record_that_leaks_a_credential_is_refused():
    verdict = verify.validate(
        _record(
            [
                {
                    "stage": "tts",
                    "provider": "openai",
                    "status": "ok",
                    "checked_at": "2026-11-18T00:00:00Z",
                    "detail": "used sk-abcdefghijklmnopqrstuvwxyz123456",
                },
            ]
        )
    )
    assert verdict["ok"] is False
    assert any("looks like a credential" in problem for problem in verdict["problems"])


def test_importing_a_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(verify.VerifyError, match="no verification record"):
        verify.import_record(tmp_path / "absent.json")


def test_importing_invalid_json_is_a_clear_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(verify.VerifyError, match="not valid JSON"):
        verify.import_record(path)


def test_importing_refuses_a_record_that_does_not_validate(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(_record([{"stage": "tts", "provider": "nope", "status": "ok"}])),
        encoding="utf-8",
    )
    with pytest.raises(verify.VerifyError, match="unknown adapter"):
        verify.import_record(path)


def test_an_import_can_be_dry_run(tmp_path):
    path = tmp_path / "good.json"
    path.write_text(
        json.dumps(
            _record(
                [
                    {
                        "stage": "tts",
                        "provider": "openai",
                        "status": "ok",
                        "checked_at": "2026-11-18T00:00:00Z",
                    },
                ]
            )
        ),
        encoding="utf-8",
    )
    verdict = verify.import_record(path, write=False)
    assert verdict["ok"] is True and verdict["written"] is None


# ----------------------------------------------------------------- status


def test_status_counts_ok_rows_only(monkeypatch, tmp_path):
    path = tmp_path / "v.json"
    verify.save(
        _record(
            [
                {
                    "stage": "tts",
                    "provider": "openai",
                    "status": "ok",
                    "checked_at": "2026-11-18T00:00:00Z",
                },
                {"stage": "tts", "provider": "sarvam", "status": "failed", "detail": "401"},
            ]
        ),
        path,
    )
    monkeypatch.setattr(verify, "RECORD", path)
    data = verify.status()
    assert data["verified"] == 1 and data["checked"] == 2


def test_a_missing_or_corrupt_record_means_zero_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "RECORD", tmp_path / "absent.json")
    assert verify.status()["verified"] == 0
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(verify, "RECORD", corrupt)
    assert verify.status()["verified"] == 0
