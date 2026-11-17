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


# ---------------------------------------------------------------- adapter contracts


def test_network_adapters_are_not_marked_verified_without_live_testing():
    """`verified` must mean "exercised against the live API", not "looks right"."""
    network = {
        "deepgram",
        "elevenlabs",
        "groq",
        "openai",
        "gemini",
        "sarvam",
        "deepl",
        "synclabs",
        "faster_whisper",
        "demucs",
        "ollama",
        "edge_tts",
    }
    for kind, table in registry.REGISTRY.items():
        for name, cls in table.items():
            if name not in network:
                continue
            assert cls(Settings()).spec.verified is False, (
                f"{kind}/{name} is marked verified; flip it only after a live-API test"
            )


def test_offline_adapters_are_verified():
    for kind, name in (
        ("asr", "stub"),
        ("translate", "stub"),
        ("tts", "stub"),
        ("separator", "ffmpeg_center"),
        ("separator", "none"),
        ("lipsync", "none"),
    ):
        assert registry.create(kind, name, Settings()).spec.verified is True


def test_deepl_picks_the_host_from_the_key():
    from dubforge.providers.translate import deepl_endpoint

    assert "api-free" in deepl_endpoint("abc:fx")
    assert deepl_endpoint("abc:fx").endswith("/v2/translate")
    assert "api-free" not in deepl_endpoint("plain-paid-key")


def test_glossary_is_applied_to_the_output_not_the_input():
    """Rewriting the source before translation corrupts the sentence being translated."""
    from dubforge.providers.translate import apply_glossary

    assert apply_glossary("ISRO launched a rocket", {"ISRO": "इसरो"}) == "इसरो launched a rocket"
    assert apply_glossary("nothing to pin", None) == "nothing to pin"
    assert apply_glossary("nothing to pin", {}) == "nothing to pin"


def test_sarvam_sends_the_unmodified_source_and_pins_afterwards(monkeypatch):
    from dubforge.providers import translate as translate_module

    seen: list[str] = []

    class FakeResponse:
        def json(self):
            return {"translated_text": "इसरो ने प्रक्षेपण किया"}

    def fake_post(url, **kwargs):
        seen.append(kwargs["json_body"]["input"])
        return FakeResponse()

    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    monkeypatch.setattr(translate_module, "post", fake_post)
    segments = [Segment(index=0, start=0, end=1, text="ISRO launched it")]
    provider = translate_module.SarvamTranslator(Settings(target_lang="hi"))
    provider.translate(segments, "en", "hi", glossary={"ISRO": "इसरो"})
    assert seen == ["ISRO launched it"], "the source must reach the API untouched"
    assert segments[0].target_text == "इसरो ने प्रक्षेपण किया"


def test_deepgram_request_is_url_encoded_and_parsed(monkeypatch, tmp_path):
    from dubforge.providers import asr as asr_module

    captured: dict = {}

    class FakeResponse:
        def json(self):
            return {
                "metadata": {"duration": 2.0},
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "words": [
                                        {
                                            "start": 0.0,
                                            "end": 0.5,
                                            "word": "hello",
                                            "punctuated_word": "Hello",
                                            "speaker": 0,
                                            "confidence": 0.99,
                                        },
                                        {
                                            "start": 0.5,
                                            "end": 1.2,
                                            "word": "there",
                                            "punctuated_word": "there.",
                                            "speaker": 1,
                                            "confidence": 0.95,
                                        },
                                    ]
                                }
                            ]
                        }
                    ],
                    "utterances": [
                        {"start": 0.0, "end": 0.5, "transcript": "Hello", "speaker": 0},
                        {"start": 0.5, "end": 1.2, "transcript": "there.", "speaker": 1},
                    ],
                },
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setattr(asr_module, "post", fake_post)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 100)
    transcript = asr_module.DeepgramASR(Settings(source_lang="en")).transcribe(str(audio), "en")
    assert "diarize=true" in captured["url"]
    assert "utterances=true" in captured["url"]
    assert captured["url"].startswith("https://api.deepgram.com/v1/listen?")
    assert [s.speaker for s in transcript.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert transcript.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert transcript.segments[0].words


def test_elevenlabs_tts_refuses_to_run_without_a_voice(monkeypatch, tmp_path):
    from dubforge.providers import tts as tts_module

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    provider = tts_module.ElevenLabsTTS(Settings(target_lang="hi"))
    with pytest.raises(Exception) as exc:
        provider.synthesize("hello", "", str(tmp_path / "out.wav"))
    assert "voice id" in str(exc.value)


def test_run_coroutine_works_inside_a_running_loop():
    """The MCP server calls providers from async code; asyncio.run() would explode there."""
    import asyncio

    from dubforge.providers.tts import run_coroutine

    async def work():
        return "done"

    async def outer():
        return run_coroutine(work())

    assert asyncio.run(outer()) == "done"
    assert run_coroutine(work()) == "done"


def test_edge_tts_looks_up_a_real_default_voice(monkeypatch, tmp_path):
    """`f"{locale}-Neural"` is not a voice; the default must be a listed one."""
    from dubforge.providers import tts as tts_module

    provider = tts_module.EdgeTTS(Settings(target_lang="hi"))
    monkeypatch.setattr(
        tts_module.EdgeTTS,
        "voices",
        lambda self, language="": [tts_module.Voice(id="hi-IN-SwaraNeural", name="Swara")],
    )
    assert provider._default_voice() == "hi-IN-SwaraNeural"

    monkeypatch.setattr(tts_module.EdgeTTS, "voices", lambda self, language="": [])
    with pytest.raises(Exception) as exc:
        provider._default_voice()
    assert "voice-pool" in str(exc.value)


# ------------------------------------------------------- reliability (0.2.0)


def test_status_codes_are_classified_consistently():
    from dubforge.providers.http import classify

    assert classify(429) == ("rate_limit", True)
    assert classify(401) == ("auth", False)
    assert classify(403) == ("auth", False)
    assert classify(400) == ("bad_request", False)
    assert classify(404) == ("not_found", False)
    assert classify(503) == ("server", True)
    assert classify(599) == ("server", True)
    assert classify(418) == ("unknown", False)


def test_provider_errors_carry_a_kind_and_retryability():
    from dubforge.providers.base import ProviderError

    error = ProviderError("nope", kind="rate_limit", status=429, retryable=True)
    assert error.to_dict()["kind"] == "rate_limit"
    assert error.to_dict()["retryable"] is True
    assert ProviderError("plain").kind == "unknown"


def test_health_without_live_makes_no_network_call(monkeypatch):
    """`live=False` must be safe to call on a plane."""
    from dubforge.providers import http

    def explode(*args, **kwargs):
        raise AssertionError("health(live=False) made a network call")

    monkeypatch.setattr(http, "get", explode)
    provider = registry.create("asr", "deepgram", Settings())
    report = provider.health(live=False)
    assert report["live"] is None
    assert "readiness only" in report["detail"]


def test_health_live_reports_a_missing_key_without_calling(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    provider = registry.create("asr", "deepgram", Settings())
    report = provider.health(live=True)
    assert report["ready"] is False
    assert report["live"] is False
    assert "DEEPGRAM_API_KEY" in report["detail"]


def test_health_live_turns_a_failure_into_a_report(monkeypatch):
    from dubforge.providers import http

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fail(*args, **kwargs):
        from dubforge.providers.base import ProviderError

        raise ProviderError("bad key", kind="auth", status=401)

    monkeypatch.setattr(http, "probe_json", fail)
    from dubforge.providers import tts as tts_module

    monkeypatch.setattr(tts_module, "probe_json", fail, raising=False)
    provider = tts_module.OpenAITTS(Settings())
    report = provider.health(live=True)
    assert report["live"] is False
    assert "auth" in report["detail"]


def test_the_chain_is_ordered_and_named_providers_are_singletons():
    chain = registry.resolve_chain("separator", Settings())
    assert [p.name for p in chain] == ["ffmpeg_center", "none"]
    single = registry.resolve_chain("tts", Settings(), "stub")
    assert len(single) == 1 and single[0].name == "stub"


def test_a_named_provider_that_is_not_ready_is_refused_not_skipped():
    with pytest.raises(Exception) as exc:
        registry.resolve_chain("asr", Settings(), "deepgram")
    assert "DEEPGRAM_API_KEY" in str(exc.value)
