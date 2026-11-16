## What this changes

<!-- One paragraph. What was wrong, what you did, what a user now sees. -->

## Why

<!-- If it is a quality change, describe the dub that went wrong. If it is a provider,
     say which vendor and whether you tested it against a live key. -->

## Checklist

- [ ] `python -m pytest -q` passes
- [ ] `ruff check src tests` and `ruff format --check src tests` pass
- [ ] New behaviour has a test that would fail without the change
- [ ] No key, token or private path appears in code, logs, docs or fixtures
- [ ] A failed dub still fails loudly (no silent fallback to a stub)
- [ ] If this adds a provider: `Spec` has `title`, `quality`, `docs`, and an honest
      `verified` value, plus a row in `docs/providers.md`
- [ ] If this changes QC: the check's severity (`fail` vs `warn`) is deliberate, and
      `docs/quality.md` is updated
- [ ] The video stream is still never re-encoded

## How you tested it

<!-- Which providers, which language pair, how long was the file, what did QC say?
     "It ran" is not a test; "QC passed, 214/214 lines, worst overrun 0.18s" is. -->
