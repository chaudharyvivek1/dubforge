# 0.9.0 release checklist

The ledger becomes authoritative: the record decides what is verified, a row expires, and a
claim with no evidence is reported as a contradiction.

## 1. The bug this milestone fixes

0.8.0 shipped the ledger and the import path, and a test that ties `verified` to `ok` rows. But
`spec.verified` was a static field: nothing read the record. A `docs/verification.json` full of
`ok` rows would have changed exactly nothing — `capabilities` would still say `verified: false`,
`doctor` would still report zero, and the pipeline would still warn about every adapter.

The test passed because the record was empty. That is the failure mode this release removes:
a mechanism that only works when it is not needed.

## 2. What shipped

* `verify.state_for(kind, name, declared, local, max_age_days, now)` returns a `State` with
  `verified`, `status`, `checked_at`, `age_days`, `stale`, `source` and `contradiction`.
* `verify.effective()`, `verify.all_states()`, and a `status()` that counts live rows, local
  adapters, stale rows and contradictions separately.
* `capabilities` provider rows and `_feature()` report the effective state plus a `verification`
  object; `doctor` reports stale and contradiction counts; the pipeline's warning is now
  `_verification_note()`, silent for proven adapters.
* `Spec.local` — an explicit marker for adapters proven by the test suite rather than by a live
  call. Set on the stub, the transcript reader, ffmpeg centre-removal and the two "none"
  providers.

## 3. The two rules, and why they are not the same rule

**Expiry.** A verification is a fact with a date. Vendors change endpoints, models and prices;
a row from another season is evidence of another system. After 180 days (`MAX_AGE_DAYS`) a row
stops counting, is reported as `stale` with its age, and says "re-run to renew".

**Local versus network.** A local adapter runs in this process or in ffmpeg, so the suite
exercises it on every commit — its declaration is evidence, labelled `source: "local"`. A network
adapter has no such proof and needs a dated row. Blending the two would let "we ran it in a test"
pose as "we called the vendor".

## 4. What the resolver found immediately

`separator/ffmpeg_center` declared `verified=True` and was neither marked local nor recorded, so
the first run of `verify.status()` reported **one contradiction**. The model had no way to say
what that flag meant. It is now `local=True`, which is what it always was — and the contradiction
count is zero, with a test that keeps it there
(`test_the_real_registry_has_no_contradictions`).

That is the argument for deriving a flag rather than setting it: the derivation found a fact the
declaration had been hiding.

## 5. Compatibility

* No public name changed. `Spec` gained `local` (default False), which only affects reporting.
* `capabilities` provider rows gained `verification`; `verified` keeps its meaning but is now
  effective rather than declared.
* `verify.status()` gained keys; the old ones (`verified`, `checked`, `record`, `note`) are
  unchanged in meaning.
* With an empty record, every surface reports what it reported before: zero verified.

## 6. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       645 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok — "0 verified, 1 local proven by the suite"
python scripts/ci_demo_check.py           passed
python -m dubforge capabilities           rows carry verification state
python -m build + twine check             PASSED
```

## 7. Honest state

`docs/verification.json` still has zero rows. Every network adapter is unverified and says so
with its reason. The difference is that the machinery now works: a dated row would flip
`capabilities`, silence the pipeline warning, and expire on its own after 180 days.

## 8. Recommended next milestone

Produce the first row. With a key present: `dubforge verify run --yes-live`, commit the record,
and watch `capabilities` change for the first time. Then `scripts/calibrate.py --measure` to put
measured speech rates behind the estimates. The remaining engineering is small; the evidence is
what is missing.
