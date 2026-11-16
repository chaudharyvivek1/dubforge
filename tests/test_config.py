"""Settings: environment parsing that cannot silently produce the wrong type."""

import pytest

from dubforge.config import Settings, is_language_code


def test_optional_float_env_is_a_float_not_a_string(monkeypatch):
    """The bug: `DUBFORGE_SPEECH_CPS=12.5` arrived as "12.5" and broke the fit maths."""
    monkeypatch.setenv("DUBFORGE_SPEECH_CPS", "12.5")
    settings = Settings.from_env()
    assert settings.speech_cps == 12.5
    assert isinstance(settings.speech_cps, float)


@pytest.mark.parametrize(
    ("var", "value", "field", "expected"),
    [
        ("DUBFORGE_MAX_SPEED", "1.2", "max_speed", 1.2),
        ("DUBFORGE_REWRITE_ROUNDS", "3", "rewrite_rounds", 3),
        ("DUBFORGE_KEEP_BACKGROUND", "false", "keep_background", False),
        ("DUBFORGE_KEEP_BACKGROUND", "yes", "keep_background", True),
        ("DUBFORGE_CACHE", "0", "cache", False),
        ("DUBFORGE_VOICE_POOL", "a, b ,c", "voice_pool", ["a", "b", "c"]),
        ("DUBFORGE_TARGET_LANG", "ta", "target_lang", "ta"),
    ],
)
def test_env_types_round_trip(monkeypatch, var, value, field, expected):
    monkeypatch.setenv(var, value)
    assert getattr(Settings.from_env(), field) == expected


def test_json_env_for_dicts(monkeypatch):
    monkeypatch.setenv("DUBFORGE_GLOSSARY", '{"ISRO": "इसरो"}')
    assert Settings.from_env().glossary == {"ISRO": "इसरो"}


def test_a_malformed_variable_warns_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("DUBFORGE_REWRITE_ROUNDS", "two")
    settings = Settings.from_env()
    assert settings.rewrite_rounds == 2  # the default
    assert settings.env_warnings
    assert "DUBFORGE_REWRITE_ROUNDS" in settings.env_warnings[0]
    assert "two" in settings.env_warnings[0]


def test_language_codes():
    assert is_language_code("hi")
    assert is_language_code("ta-IN")
    assert is_language_code("pt-BR")
    assert not is_language_code("Hindi")
    assert not is_language_code("hi there")
    assert not is_language_code("")
    assert is_language_code("auto", allow_auto=True)
    assert not is_language_code("auto")


def test_validate_reports_actionable_problems():
    assert Settings(target_lang="hi").validate() == []
    problems = Settings(target_lang="Hindi (India)").validate()
    assert any("language code" in p for p in problems)
    assert any("max_speed" in p for p in Settings(max_speed=-1).validate())
    assert any("loudness_lufs" in p for p in Settings(loudness_lufs=0).validate())
    assert any("rewrite_rounds" in p for p in Settings(rewrite_rounds=99).validate())
    assert any("speech_cps" in p for p in Settings(speech_cps=0.1).validate())


def test_with_overrides_ignores_unknown_keys():
    base = Settings(target_lang="hi")
    changed = base.with_overrides(target_lang="ta", not_a_field=1)
    assert changed.target_lang == "ta"
    assert not hasattr(changed, "not_a_field")


def test_settings_from_json_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"target_lang": "bn", "max_speed": 1.05}', encoding="utf-8")
    settings = Settings.load(path)
    assert settings.target_lang == "bn"
    assert settings.max_speed == 1.05


def test_describe_contains_no_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear")
    described = Settings.from_env().describe()
    assert "sk-must-not-appear" not in str(described)
