"""The registry contract: every adapter declares itself, and nothing leaks a key."""

import json

import pytest

from dubforge.config import Settings
from dubforge.media import Segment
from dubforge.providers import registry
from dubforge.providers.base import ProviderUnavailable
from dubforge.providers.translate import build_prompt, parse_response


def test_every_registered_adapter_has_a_matching_spec():
    for kind, table in registry.REGISTRY.items():
        for name, cls in table.items():
            provider = cls(Settings())
            assert provider.spec.kind == kind, (
                f"{name} registered under {kind} but says {provider.spec.kind}"
            )
            assert provider.spec.name, f"{name} has no spec name"
            assert provider.spec.title, f"{name} has no title"
            assert provider.spec.quality, f"{name} must say honestly what it is good at"


def test_auto_order_only_names_real_adapters():
    for kind, order in registry.AUTO.items():
        for name in order:
            assert name in registry.REGISTRY[kind], f"{kind}/{name} is in AUTO but not registered"


def test_describe_never_includes_secret_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-appear")
    rows = registry.all_specs(Settings())
    blob = json.dumps(rows)
    assert "sk-should-never-appear" not in blob
    openai_asr = next(r for r in rows if r["kind"] == "asr" and r["name"] == "openai")
    assert openai_asr["available"] is True
    assert openai_asr["missing"] == []


def test_missing_key_is_reported_with_the_variable_name(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    provider = registry.create("asr", "deepgram", Settings())
    assert provider.available is False
    assert any("DEEPGRAM_API_KEY" in gap for gap in provider.missing())
    with pytest.raises(ProviderUnavailable):
        provider.require()


def test_unknown_provider_lists_the_alternatives():
    with pytest.raises(ProviderUnavailable) as exc:
        registry.create("tts", "nope", Settings())
    assert "elevenlabs" in str(exc.value)


def test_resolve_named_provider_requires_readiness():
    with pytest.raises(ProviderUnavailable):
        registry.resolve("asr", Settings(), "deepgram")


def test_resolve_auto_picks_a_ready_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    provider = registry.resolve("separator", Settings())
    assert provider.name in {"demucs", "ffmpeg_center", "none"}
    assert provider.available


def test_stub_providers_are_always_available():
    for kind, name in (("asr", "stub"), ("translate", "stub"), ("tts", "stub")):
        provider = registry.create(kind, name, Settings())
        assert provider.available, f"{kind}/stub should never need a key"


def test_prompt_states_the_constraint_and_the_language():
    segments = [
        Segment(index=0, start=0.0, end=1.0, text="Hello", window=1.0),
        Segment(index=1, start=1.0, end=2.0, text="World", window=1.0),
    ]
    prompt = build_prompt(
        segments, "en", "hi", {"ISRO": "इसरो"}, {1: "must fit 1.0s"}, "a science film"
    )
    assert "into hi" in prompt
    assert "इसरो" in prompt
    assert "must fit 1.0s" in prompt
    assert "a science film" in prompt
    assert '"i": 0' in prompt


def test_parse_response_handles_fences_and_preamble():
    raw = 'Sure!\n```json\n[{"i": 0, "t": "नमस्ते"}, {"i": 1, "t": "दुनिया"}]\n```\n'
    segments = [
        Segment(index=0, start=0, end=1, text="a"),
        Segment(index=1, start=1, end=2, text="b"),
    ]
    mapping = parse_response(raw, segments)
    assert mapping == {0: "नमस्ते", 1: "दुनिया"}


def test_parse_response_rejects_junk():
    from dubforge.providers.base import ProviderError

    with pytest.raises(ProviderError):
        parse_response("no json here", [Segment(index=0, start=0, end=1, text="a")])
