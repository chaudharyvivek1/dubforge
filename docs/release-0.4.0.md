# 0.4.0 release checklist

Incremental processing, honest job identity, and a review loop that a person can actually
use. No public name changed; three MCP tools and two CLI commands were added.

## 1. Audit findings (verified before any code changed)

| # | Finding | Severity | Reproduction |
|---|---|---|---|
| 1 | `source_fingerprint()` was `size:mtime_ns`, so different content could reuse a job | **P0** | two files of equal size with equal `utime` → identical fingerprint |
| 2 | `retranslate` was missing from `REQUEST_FIELDS` | **P0** | `request_fingerprint(retranslate=True) == request_fingerprint(False)` |
| 3 | A supplied transcript was identified by path, not content | **P0** | same path, rewritten bytes → same request hash |
| 4 | The segment lock was taken **after** synthesis | **P1** | `pipeline.py`: `try_stage("tts", …)` then `store.claim(...)` — two misses, two paid calls |
| 5 | `docs/assessment.md` claimed features were missing that 0.2.x/0.3.0 shipped | P1 | "per-segment regeneration **missing**", "line-length limits **missing**" |
| 6 | `ROADMAP.md` described 0.2.1 as current | P1 | "## Now — the honest state of 0.2.1" |
| 7 | Higher-level locking already prevented duplicate synthesis for *the same request* | — | `workspace.open_job` holds a per-job lock for the whole run |

Finding 7 is the boundary that decides where the fix belongs: the workspace lock serialises
**the same job** across processes, so the segment lock is not a duplicate of it. It is needed
for (a) two jobs of the same source in different languages, (b) concurrent regeneration, and
(c) two threads inside one job — and those are the cases the new tests cover.

## 2. Design decisions

* **Content is identity.** Hashing costs one read of the file per job; the memo table makes it
  once per process. Metadata identity was cheaper and wrong.
* **A lock is stale when its owner is gone, not when it is old.** An age-first check lets a
  second worker rob a slow but live renderer — the same duplicate spend, disguised.
* **The raw render is cached, never the mix.** Stretch is milliseconds; synthesis is money.
  Ducking follows the dialogue, so the mix is always rebuilt and the report says so.
* **The review file is data, not a command.** It can change text, voice and which lines to
  re-render; it cannot reach paths, tokens or credentials, and a stale file is refused.
* **Publishing is a separate step.** Mux to `.partial`, validate, `os.replace`. Overwriting is
  opt-in.

## 3. Changed files

New: `src/dubforge/stems.py`, `src/dubforge/review.py`, `tests/test_identity.py`,
`tests/test_segment_concurrency.py`, `tests/test_stems.py`, `tests/test_review_workflow.py`,
`tests/test_output_safety.py`, `docs/release-0.4.0.md`.
Changed: `jobs.py`, `pipeline.py`, `segments.py`, `config.py`, `cli.py`, `server.py`,
`scripts/benchmark.py`, generated references, README/ROADMAP/CHANGELOG/architecture/assessment,
skill.

## 4. Job identity migration

Old job ids were `<stem>-<pathhash>-<requesthash>` where the request hash was built on
`size:mtime`. New ids are the same shape with a content-based request hash, so **existing job
directories keep working** (they are read by id) and new submissions of an old source get a new
id. `Job` records `source_fingerprint`; legacy records without it still load (`review export`
then refuses with "no review.json yet", not a crash). Nothing is migrated or deleted.

## 5. Cache compatibility

* Segment stores from 0.3.0 are read as-is (same record shape, new `meta` keys ignored).
* The stem cache is new; a missing entry is a miss, never an error.
* A workspace with no `cache/stems` separates once and writes it.

## 6. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       566 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed
python scripts/benchmark.py --sizes small measured
python scripts/benchmark.py --regen --sizes small  full run vs 0/1/5/10% edits
python -m build + twine check             PASSED
```

## 7. Benchmark (small clip, stub providers)

| case | TTS calls | separations | reused renders | bed reused | wall |
|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | 11.6 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | 9.3 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | 9.2 s |
| regen, 3 edited | 3 | 0 | 0 | 1 | 7.7 s |

The wall clock barely moves because the mux and mix dominate on a 9 s clip with stub
providers; the TTS and separation counts are the numbers that scale with film length.

## 8. Provider verification

Unchanged: `docs/verification.json` is empty, every network adapter is `verified: false`, and
no test or CI run makes a paid request.

## 9. Remaining limitations

* Remixing is whole-track. Partial remixing (reuse the mixed bed, re-place only changed lines)
  is the next milestone and is the last big win left.
* Word-level alignment is provider-dependent and unverified live.
* Calibration has no measured samples.
* Hashing a very large source costs one read per job.
* Review voice overrides work at speaker granularity, which is what the voice stage supports.

## 10. Recommended next milestone

Partial remixing plus a review UI-free diff: keep the mixed dialogue track, splice only the
changed regions, and re-run ducking locally.
