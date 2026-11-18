# 0.8.0 release checklist

The verification ledger: making the one blocked item executable, auditable and importable.

## 1. Why this milestone, and what it cannot do here

0.7.0 recommended live verification as the next milestone. It needs two things this
environment does not have: provider API keys, and network access to the vendors. Nothing in
this release claims otherwise — `docs/verification.json` still has zero rows.

What *can* be built without a key is the machinery that makes verification a procedure rather
than an intention: a plan that says exactly what would run and what it costs, a run that
refuses to spend money without an explicit flag, an import path so evidence gathered elsewhere
can be committed, and validation strict enough that a fabricated record is harder to write than
a real one.

## 2. What shipped

* `src/dubforge/verify.py` — `plan`, `run`, `verify_one`, `import_record`, `validate`, `status`,
  `load`, `save`, `render_plan`. `scripts/verify_providers.py` is now a thin wrapper, so the
  behaviour a maintainer runs from a terminal and the behaviour the tests exercise are the same
  code.
* `dubforge verify plan|run|import|status` and the MCP tool `dubforge_verification_status`
  (27 tools, read-only).
* `docs/verification.md` — the procedure, the meaning of a green row, and the state today.
* `doctor` reports the ledger in text and JSON.

## 3. The validation rules, and why each exists

| rule | the failure it prevents |
|---|---|
| schema version known | an old or invented record silently influencing `verified` |
| `stage/provider` must exist in the registry | a typo or an invented adapter looking like evidence |
| no duplicate adapters | two rows disagreeing about one provider |
| status from a fixed set | `"yes"`, `"passed"`, `"ok!"` and other unparseable claims |
| ISO date on every `ok` row | an undated claim, which cannot be audited or expired |
| credential tripwire | a record committed with a key in a `detail` field |

The tripwire is a heuristic and says so in the code: it catches the accident (a pasted key),
not a determined forger. The real protection is that a row must name a provider that exists and
a date, and that a test ties `verified` to `ok` rows.

## 4. Correctness evidence

* `test_the_plan_asks_about_readiness_but_calls_nothing` — a spy adapter raises if `plan` ever
  calls `transcribe`, `translate`, `synthesize` or `voices`.
* `test_running_without_an_opt_in_is_refused` — no request without `--yes-live`.
* `test_a_written_record_never_contains_a_key` — the key is set in the environment and must not
  appear in the payload.
* `test_an_ok_row_without_a_date_is_refused`, `test_a_record_that_leaks_a_credential_is_refused`,
  `test_an_unknown_adapter_is_refused`, `test_a_duplicate_row_is_refused`.
* `test_status_counts_ok_rows_only` and the corrupt-record case.

## 5. Compatibility

* No public name changed. `scripts/verify_providers.py` keeps its flags and gains `--yes-live`
  as a hard requirement (it was already required for a live run; the error is now explicit).
* `docs/verification.json` gained an optional `schema` key; older records without it are read as
  schema 1.
* The MCP tool count is 27; the reference docs are regenerated.

## 6. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       629 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok (reports 0 verified)
python scripts/ci_demo_check.py           passed
python -m dubforge verify plan            table rendered, no requests
python -m dubforge verify run --only tts  refused without --yes-live
python -m build + twine check             PASSED
```

## 7. Honest state

```
$ dubforge verify status
0 verified, 0 row(s) in the record
```

Every network adapter is unverified. `data/calibration/samples.jsonl` is empty. Every run
reports sentence-level timing only.

## 8. Recommended next milestone

Run it. With keys in the environment: `dubforge verify run --yes-live`, commit the record, flip
`verified` only where the record says `ok`, then `scripts/calibrate.py --measure` to replace the
estimated speech rates with measured ones. The engineering that remains is small; the evidence
is what is missing, and only someone with keys can produce it.
