# Contributing

The most useful thing you can send is a **failing QC report**: `qc.json`, the command
you ran, and one sentence about what sounded wrong. That single artefact usually
identifies whether the bug is timing, voice mapping, the bed or the translator.

## Setup

```bash
git clone https://github.com/chaudharyvivek1/dubforge.git
cd dubforge
pip install -e ".[dev,mcp]"
python -m dubforge doctor      # must be green before anything else
python -m pytest -q            # 56 tests, about 100 seconds
```

Tests that need ffmpeg skip themselves when it is absent, so a bare checkout still
runs the pure-Python half.

## What is worth a pull request

* **A new provider.** One file under `src/dubforge/providers/`, a `Spec`, one line in
  `REGISTRY`, one row in `docs/providers.md`. Leave `verified=False` unless you have
  run it against the live API and can describe the output.
* **A better speech-rate constant.** `fit.CPS` is empirical. If you have measured
  characters-per-second on real narration in a language, say which language, what
  material, and how you measured it.
* **A QC check.** If a dub failed in a way the report did not catch, that is a missing
  check. New checks need a failing case in the tests.
* **A platform fix.** Windows paths, old ffmpeg builds, ARM — all welcome, all
  reproducible.

## What will be pushed back on

* Anything that makes a failed dub look successful. Silent fallbacks to stubs, QC
  checks that warn when they should fail, exit codes that lie.
* Anything that logs, prints or serialises a key.
* A provider adapter with no `quality` sentence — the field is how a stranger chooses
  between two APIs, and the test suite enforces it.
* Re-encoding the video stream. The picture is a passenger; keep it that way.

## Style

* `ruff check` and `ruff format` must pass (the config is in `pyproject.toml`).
* Comments explain *why*, especially where a number was chosen. `# 1.15` is a
  decision; `# cap the speed` is noise.
* Docstrings on modules say what the module prevents, not what it contains.

## Adding a provider, in full

```python
from dubforge.providers.base import Spec, Translator
from dubforge.providers.http import post

class MyTranslator(Translator):
    spec = Spec(
        name="mine",
        kind="translate",
        title="My translation API",
        env=("MY_API_KEY",),
        capabilities=("duration_aware", "glossary"),
        languages="en, hi, ta",
        verified=False,
        quality="Cheap and fast; loses idiom on dense dialogue.",
        notes="Set DUBFORGE_TRANSLATE_MODEL to pin a model.",
        docs="https://example.com/docs",
    )

    def translate(self, segments, source, target, glossary=None, instructions=None, context=""):
        self.require()
        from dubforge.providers.translate import build_prompt, parse_response

        prompt = build_prompt(segments, source, target, glossary, instructions, context)
        resp = post("https://api.example.com/v1/translate",
                    headers={"Authorization": f"Bearer {self.settings.provider_key('mine')}"},
                    json_body={"prompt": prompt},
                    provider="mine")
        for seg in segments:
            ...
```

Reusing `build_prompt` means your provider inherits the craft rules (say what it
means, fit the time, stay in character) instead of reinventing them badly.

## Tests

| file | covers |
|---|---|
| `test_fit.py` | the timing arithmetic — verdicts, character budgets, constraint text |
| `test_timeline.py` | segmentation, speaker assignment, windows, voice mapping |
| `test_srt.py` | subtitle round-trips and the transcript model |
| `test_providers.py` | the registry contract, secret hygiene, prompt/response parsing |
| `test_pipeline.py` | end to end on a real ffmpeg: demo clip, dub, QC, stretching |
| `test_cli.py` | the CLI's honesty: exit codes, JSON output, no key leakage |

`test_pipeline.py::test_qc_fails_a_stub_translation_honestly` is the one to read first.
It asserts that a pipeline test never looks like a finished dub — the whole design
follows from that.

## Licence

Contributions are MIT, same as the project.
