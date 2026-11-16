"""Settings: environment parsing that cannot silently produce the wrong type."""

import json

import pytest

from dubforge.config import ConfigError, Settings, is_language_code


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


# ------------------------------------------------------- strict boolean parsing

BOOL_FIELDS = [
    "keep_background",
    "duck",
    "cache",
    "dry_run",
    "keep_work",
]


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("No", False),
        ("off", False),
    ],
)
def test_every_boolean_field_parses_the_documented_words(monkeypatch, field, value, expected):
    monkeypatch.setenv(f"DUBFORGE_{field.upper()}", value)
    assert getattr(Settings.from_env(), field) is expected


@pytest.mark.parametrize("value", ["flase", "maybe", "2", "-1", "yes please", "tru"])
def test_an_unparseable_boolean_warns_and_keeps_the_default(monkeypatch, value):
    """A typo must never silently disable caching, ducking or background preservation."""
    monkeypatch.setenv("DUBFORGE_KEEP_BACKGROUND", value)
    settings = Settings.from_env()
    assert settings.keep_background is True  # the default, not False
    assert any("DUBFORGE_KEEP_BACKGROUND" in warning for warning in settings.env_warnings)


# ---------------------------------------------------- configuration file validation


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("{not json", "not valid JSON"),
        ("[1, 2, 3]", "JSON object at the top level"),
        ('{"targt_lang": "hi"}', "unknown key"),
        ('{"max_speed": "fast"}', "must be a number"),
        ('{"keep_background": "yes"}', "must be true or false"),
        ('{"rewrite_rounds": 2.5}', "must be an integer"),
        ('{"voice_pool": "a,b"}', "must be a list"),
        ('{"glossary": ["ISRO"]}', "must be an object"),
        ('{"max_speed": NaN}', "not a valid number"),
        ('{"max_speed": Infinity}', "not a valid number"),
        ('{"max_speed": 9.0}', "invalid configuration"),
        ('{"target_lang": "Hindi (India)"}', "invalid configuration"),
    ],
)
def test_configuration_errors_are_actionable(tmp_path, body, fragment):
    path = _write(tmp_path, "settings.json", body)
    with pytest.raises(ConfigError) as exc:
        Settings.load(path)
    message = str(exc.value)
    assert fragment in message
    assert "Traceback" not in message


def test_a_missing_configuration_file_says_so(tmp_path):
    with pytest.raises(ConfigError) as exc:
        Settings.load(tmp_path / "nope.json")
    assert "not found" in str(exc.value)


def test_an_empty_configuration_file_is_valid(tmp_path):
    path = _write(tmp_path, "empty.json", "")
    assert Settings.load(path).target_lang == "en"


def test_a_valid_configuration_file_still_loads(tmp_path):
    path = _write(
        tmp_path,
        "settings.json",
        json.dumps(
            {
                "target_lang": "ta",
                "max_speed": 1.05,
                "keep_background": False,
                "voice_pool": ["a", "b"],
                "glossary": {"ISRO": "இஸ்ரோ"},
            }
        ),
    )
    settings = Settings.load(path)
    assert settings.target_lang == "ta"
    assert settings.max_speed == 1.05
    assert settings.keep_background is False
    assert settings.voice_pool == ["a", "b"]
    assert settings.glossary == {"ISRO": "இஸ்ரோ"}


def test_a_configuration_file_cannot_smuggle_an_unknown_field(tmp_path):
    path = _write(tmp_path, "settings.json", '{"openai_api_key": "sk-in-a-file"}')
    with pytest.raises(ConfigError) as exc:
        Settings.load(path)
    assert "unknown key" in str(exc.value)
    assert "sk-in-a-file" not in str(exc.value)
