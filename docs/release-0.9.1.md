# 0.9.1 release checklist

Polling reliability, timeout correctness and error classification.

## 1. Findings — was the suspected defect real?

The brief suspected that the 0.9.0 retry loop ignored the caller's timeout. It did, and the
underlying cause was worse than a loop bug. Three defects reproduced on the base commit:

| # | Reproduction | Result on `c8a6b6c` |
|---|---|---|
| 1 | `wait(job_id, timeout=0)` on an **existing, running** job | `JobError: no job '<id>'` — a lie about a job that exists |
| 2 | write `{not json` into a record, then `status()` | `JobError: no job '<id>'` — a corrupt record reported as absent |
| 3 | `chmod 000` a record, then `status()` | `JobError: no job '<id>'` — an unreadable record reported as absent |

Root cause: `JobStore.get()` returned `None` for *four* different situations — absent, unreadable
(any `OSError`), corrupt JSON, wrong shape — and `status()` flattened all of them into
`no job '<id>'`. The 0.9.0 fix then string-matched `"no job"` and retried, which:
treated a Windows sharing violation as a missing file (right outcome, wrong reason), retried a
corrupt record for up to ten seconds as if it might heal, and reported an existing job as missing
when `timeout=0`.

The deadline was also measured with `time.time()` and slept in fixed 100/250 ms steps, so a 50 ms
timeout could overrun its own budget.

## 2. Changes

* `JobError` gained `kind` (`missing` | `unreadable` | `corrupt` | `job`), defaulting to `job` so
  every existing `except JobError` keeps working. Messages are unchanged for existing cases.
* `JobStore.get(..., strict=False)` — the lenient default is preserved for callers that want "the
  job or nothing"; `strict=True` raises a classified error instead of returning `None`.
  `JobRunner.status()` uses the strict form.
* `wait()`: monotonic deadline, sleeps clamped to the remaining budget, retries **only**
  `kind == "missing"` and only while time remains, propagates everything else on the first read,
  checks once at `timeout=0`, and preserves the exact `no job '<id>'` error for an id that never
  appears.
* Every `no job` raise in `jobs.py` is classified (five sites: `update`, `claim`, `cancel`,
  `retry`, `settings_for`, plus the poll loop), so a caller can branch on the kind anywhere.

## 3. What was investigated and deliberately not changed

**Is `os.replace` racy?** No evidence found. `save()` writes a temp file and renames it; a stress
test that replaces the record 200 times while a reader reads it never observed a half-written
record. The 0.9.0 Windows failure is therefore best explained as a *sharing violation* (an
`OSError` that `get()` swallowed), not a missing-file window. No lock was added: the fix that the
evidence supports is classification, and adding a lock would have hidden the real signal.

## 4. Tests

`tests/test_wait_deadline.py` (19), covering the brief's A–H:

| test | what it pins |
|---|---|
| A | a 1 s timeout on a nonexistent id: precise error, elapsed measured on a monotonic clock, `< 2 s` |
| A | a 50 ms timeout is not overrun by a sleep |
| A | `timeout=0` on a running job is a timeout, not "no job"; on a finished job it returns |
| B | three transient misses then success |
| C | persistent misses still respect a 0.6 s timeout |
| E | repeated misses do not reset the deadline |
| D | a never-created id raises `no job '<id>'` |
| F | completion, failure and cancellation during polling |
| G | corrupt and unreadable records propagate immediately; unexpected errors propagate |
| H | `get()` lenient by default, strict on request; `JobError` keeps its shape; a concurrent writer never yields a half-written record |

One existing test was updated rather than weakened: the 0.9.0 transient-miss double raised an
*unclassified* `JobError`, which is now deliberately not retried — so it was simulating a
different error than the one it claimed to. It now raises `kind="missing"`, like the real code.

## 5. Verification ledger (Phase 6)

Untouched. `verify.state_for()`, `verify.status()`, local/network classification, expiry,
capabilities, `doctor` and the pipeline warning are unaffected — their tests all pass unchanged.

## 6. Quality gates (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       666 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed
python -m build + twine check             PASSED
```

## 7. Honest status

Implemented and verified locally, committed and pushed; CI results are reported in the message
that follows the workflow run. Nothing in this release claims a live provider check.

## 8. Known limitations

* `update()` serialises writers within a process (`self._lock`); two *processes* updating one job
  concurrently could still overwrite each other's fields. The claim model means one worker owns a
  job, so this is a documented boundary rather than a bug — but it is not a cross-process lock.
* `wait()` polls at 250 ms; a job finishing mid-interval is noticed on the next poll.
