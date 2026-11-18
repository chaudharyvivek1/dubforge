"""The ledger decides, the spec only declares — and a claim with no evidence is caught.

0.8.0 wrote the record; nothing read it. This module's tests prove the wiring: a dated `ok` row
makes an adapter verified, an old row expires, and a network adapter that declares itself
verified without a row is reported as a contradiction rather than believed.
"""

import time

from dubforge import verify
from dubforge.config import Settings


def _record(rows) -> dict:
    return {"schema": verify.SCHEMA, "generated_at": "2026-11-18T00:00:00Z", "results": rows}


def _ok(stage="tts", provider="openai", days_ago=1) -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))
    return {
        "stage": stage,
        "provider": provider,
        "status": "ok",
        "checked_at": stamp,
        "detail": "audio rendered",
    }


# --------------------------------------------------------------- the resolver


def test_a_fresh_ok_row_verifies_a_network_adapter():
    state = verify.state_for("tts", "openai", _record([_ok(days_ago=1)]))
    assert state.verified is True
    assert state.source == "record"
    assert state.age_days == 1
    assert "verified" in state.note


def test_an_old_ok_row_expires():
    state = verify.state_for("tts", "openai", _record([_ok(days_ago=verify.MAX_AGE_DAYS + 5)]))
    assert state.verified is False
    assert state.stale is True
    assert state.status == "stale"
    assert "re-run to renew" in state.note


def test_the_expiry_is_configurable():
    record = _record([_ok(days_ago=30)])
    assert verify.state_for("tts", "openai", record).verified is True
    assert verify.state_for("tts", "openai", record, max_age_days=10).verified is False


def test_a_failed_row_is_not_verified_and_says_why():
    row = {
        "stage": "tts",
        "provider": "openai",
        "status": "failed",
        "checked_at": "2026-11-18T00:00:00Z",
        "detail": "401 unauthorized",
    }
    state = verify.state_for("tts", "openai", _record([row]))
    assert state.verified is False
    assert state.status == "failed"
    assert "401" in state.note


def test_an_ok_row_without_a_readable_date_is_not_trusted():
    row = {"stage": "tts", "provider": "openai", "status": "ok", "checked_at": "whenever"}
    state = verify.state_for("tts", "openai", _record([row]))
    assert state.verified is False
    assert "cannot be trusted" in state.note


def test_no_row_means_unverified_not_unknown():
    state = verify.state_for("tts", "openai", _record([]))
    assert state.verified is False
    assert state.status == "unverified"
    assert state.source == "none"


# ------------------------------------------------------- local versus network


def test_a_local_adapter_is_verified_by_the_suite_not_by_a_row():
    state = verify.state_for("separator", "ffmpeg_center", _record([]), declared=True, local=True)
    assert state.verified is True
    assert state.source == "local"
    assert "test suite" in state.note


def test_a_network_adapter_that_declares_verified_without_a_row_is_a_contradiction():
    state = verify.state_for("tts", "openai", _record([]), declared=True, local=False)
    assert state.verified is False
    assert state.contradiction is True
    assert state.source == "declared"


def test_the_real_registry_has_no_contradictions():
    """Every adapter that declares verified is local, and the record is honest."""
    for state in verify.all_states(record=_record([])):
        assert not state.contradiction, state.to_dict()


def test_every_network_adapter_is_unverified_in_a_fresh_checkout():
    states = verify.all_states(record=_record([]))
    assert states, "there must be adapters to check"
    for state in states:
        if state.source != "local":
            assert state.verified is False, state.to_dict()


# --------------------------------------------------------------- the summary


def test_status_counts_live_local_stale_and_contradictions():
    record = _record(
        [
            _ok("tts", "openai", days_ago=1),
            _ok("tts", "sarvam", days_ago=verify.MAX_AGE_DAYS + 10),
        ]
    )
    data = verify.status()
    assert data["verified"] == 0, "the repo's own record is empty; this call reads it"
    states = verify.all_states(record=record)
    live = [state for state in states if state.verified and state.source == "record"]
    assert {f"{s.stage}/{s.provider}" for s in live} == {"tts/openai"}
    stale = [state for state in states if state.stale]
    assert {f"{s.stage}/{s.provider}" for s in stale} == {"tts/sarvam"}


# ------------------------------------------------------- through the surfaces


def test_capabilities_reports_the_ledger_not_the_declaration(monkeypatch, tmp_path):
    from dubforge import capabilities

    record = _record([_ok("tts", "openai", days_ago=2)])
    monkeypatch.setattr(verify, "RECORD", tmp_path / "v.json")
    verify.save(record, tmp_path / "v.json")
    rows = capabilities.snapshot(Settings())["providers"]["tts"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["openai"]["verified"] is True
    assert by_name["openai"]["verification"]["source"] == "record"
    assert by_name["sarvam"]["verified"] is False
    assert by_name["sarvam"]["verification"]["status"] == "unverified"


def test_capabilities_expires_a_stale_row(monkeypatch, tmp_path):
    from dubforge import capabilities

    path = tmp_path / "v.json"
    verify.save(_record([_ok("tts", "openai", days_ago=verify.MAX_AGE_DAYS + 1)]), path)
    monkeypatch.setattr(verify, "RECORD", path)
    rows = capabilities.snapshot(Settings())["providers"]["tts"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["openai"]["verified"] is False
    assert by_name["openai"]["verification"]["stale"] is True


def test_the_pipeline_warns_about_unproven_adapters_only(monkeypatch, tmp_path):
    from dubforge import pipeline
    from dubforge.providers import registry

    path = tmp_path / "v.json"
    verify.save(_record([]), path)
    monkeypatch.setattr(verify, "RECORD", path)

    local = registry.create("tts", "stub", Settings())
    assert pipeline._verification_note("tts", local) == "", "a local adapter is proven"

    # A network adapter with no row: warned, and the warning says why.
    class Fake:
        name = "openai"
        spec = registry.create("tts", "openai", Settings()).spec

    note = pipeline._verification_note("tts", Fake())
    assert "unverified" in note and "never verified" in note

    # The same adapter with a fresh ok row: no warning.
    verify.save(_record([_ok("tts", "openai", days_ago=2)]), path)
    assert pipeline._verification_note("tts", Fake()) == ""


def test_doctor_reports_the_ledger(tmp_path, monkeypatch):
    from dubforge import doctor

    path = tmp_path / "v.json"
    verify.save(_record([_ok("tts", "openai", days_ago=3)]), path)
    monkeypatch.setattr(verify, "RECORD", path)
    text = doctor.text(Settings())
    assert "verification:" in text
    assert "1 adapter(s) verified against a live API" in text
