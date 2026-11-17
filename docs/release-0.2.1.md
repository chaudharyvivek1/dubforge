# 0.2.1 release checklist

A reliability release: the same product, harder to break and harder to overpay. Nothing in
the public API, CLI or MCP tool names changed.

## 1. Confirmed bugs (verified against 0.2.0 before fixing)

| # | Bug | Sev | Root cause |
|---|---|---|---|
| 1 | A live job whose provider call outran the heartbeat window was reported `interrupted` | **P0** | `_recover_one` treated a stale heartbeat as death, even on the same host with a live pid |
| 2 | One source could only ever have one job, so Hindi and Tamil collided | **P0** | job id was `<stem>-<pathhash>`: the request was not part of the identity |
| 3 | Two `JobRunner`s (or two processes) could both run the same request | **P0** | ownership was an in-memory `set`, which is process-local by construction |
| 4 | `lessons/class-a/lesson.mp4` and `lessons/class-b/lesson.mp4` wrote to one output | **P0** | `output_for` used the basename only; no relative structure, no collision handling |
| 5 | A retry could silently retry a non-idempotent POST after a timeout | **P1** | generic retry loop could not tell "rejected" from "maybe accepted" |
| 6 | A retry left the previous attempt's `output`, `qc` and `warnings` in place | **P1** | retry reset status but not result fields |
| 7 | Illegal transitions were accepted (e.g. `queued → completed`) | **P1** | no validator |
| 8 | Output paths in a non-existent folder failed at the mux, after paying for the run | **P1** | no `mkdir` for an explicit `--out` |
| 9 | Output discovery stopped at 200 jobs, so a large batch could re-dub its own results | **P1** | `store.list(limit=200)` used as an output index |
| 10 | The QC report could not see subtitle or container problems at all | **P1** | no measurable checks for readability, timing, streams, codec, layout, silence |
| 11 | A queued job with no claim was read as dead | P1 (introduced and fixed within this release) | recovery assumed every active job had been claimed |

## 2. Reproduction, before the fix

```
$ python -c "…store.update(job, status='processing', pid=os.getpid(), heartbeat_at=now-999)…"
live pid, stale heartbeat -> interrupted        # bug 1

$ new_job_id(src) with target_lang=hi and target_lang=ta
lesson-6eb3b1db == lesson-6eb3b1db              # bug 2

$ batch.output_for("…/class-a/lesson.mp4") == batch.output_for("…/class-b/lesson.mp4")
True                                            # bug 4
```

Each is now a test in `tests/test_job_reliability.py` or `tests/test_retry_safety.py`, and
each fails on the 0.2.0 code.

## 3. What changed

**Identity** — job id is `<stem>-<pathhash>-<requesthash>`; `request_fingerprint()` hashes
only the settings that change the result (target/source language, providers, models, voices,
glossary, context, style, emotion, mix and timing settings, transcript/script, fallback).
`workdir`, `cache`, logging and limits are excluded on purpose. Two-part ids still load.

**Ownership** — a claim file created with `O_EXCL` holds a token, pid, host and start time.
`update(token=…)` refuses a worker that no longer owns the job; `heartbeat()` and `release()`
are owner-only; a claim from a dead process is reclaimed, a live one is never stolen.

**Liveness** — the worker refreshes a heartbeat on a timer (`job_heartbeat_interval_s`), and
recovery prefers the pid where pids are meaningful, the heartbeat where they are not
(Windows, other hosts). A future timestamp (clock skew) counts as fresh.

**Lifecycle** — `TRANSITIONS` is enforced by `JobStore.transition()`; `retry()` creates an
identified attempt, clears the previous attempt's result fields and re-runs the *original*
request snapshot (`store.settings_for`).

**Retries** — `http.post(..., idempotent=False, idempotency_key=None)`; a network failure on
a non-idempotent call raises `kind="ambiguous"` instead of retrying; 429/5xx answers are
still retried with `Retry-After`; retries are counted and surfaced in the job, the report
and the event log. OpenAI calls pass its documented `Idempotency-Key`.

**Batch** — outputs preserve the relative folder structure, disambiguate deterministically
on collision, never replace an existing file, never equal the source, deduplicate symlinked
sources, and discover outputs without a 200-job ceiling.

**QC** — six new measurable checks (21 total): `subtitle_timing`, `subtitle_readability`
(thresholds configurable), `streams_present`, `video_codec_preserved`, `audio_layout`,
`not_mostly_silent`. All warnings except `subtitle_timing` and `streams_present`, which are
failures.

**Observability** — `DUBFORGE_EVENTS=<file>` writes append-only JSONL with stable event
names and no secrets.

## 4. Changed files

`src/dubforge/jobs.py` (rewritten), `batch.py`, `events.py` (new), `providers/http.py`,
`providers/{tts,translate}.py`, `pipeline.py`, `qc.py`, `config.py`, `server.py`, plus
`tests/test_job_reliability.py` and `tests/test_retry_safety.py` (new),
`tests/{test_jobs,test_server,test_pipeline,test_limits_batch_capabilities}.py` (updated),
the four skill references, `docs/architecture.md`, `docs/security.md`, `docs/assessment.md`,
`docs/release-0.2.1.md`, README, CHANGELOG, ROADMAP, and the regenerated
`docs/{configuration,mcp-tools,cli}.md`.

## 5. New tests (103 added, 420 total)

Heartbeat: live pid with a stale heartbeat stays `processing`; fresh heartbeat where pids
cannot be probed; stale heartbeat where pids cannot be probed; dead pid with a fresh
heartbeat; clock skew; configurable threshold; recovery after restart; two concurrent
readers leaving valid JSON. Identity: two languages, dedupe, provider change, voice/glossary
change, irrelevant settings ignored, source edit, legacy ids. Claims: single winner, dead
claim reclaimed, stale worker refused, owner-only heartbeat, release, cross-store, **a real
subprocess** that cannot claim a live job. Attempts: illegal transition refused, terminal
job cannot reactivate, `qc_failed` distinguishable, retry clears results, retry preserves the
request, bounded history. Runner: completes and releases, second runner does not double-run,
shutdown interrupts and stops heartbeat threads, retry uses the original settings. Retry
safety: 429 + `Retry-After`, 401 not retried, 5xx retried, timeout on non-idempotent not
retried, idempotent retried, idempotency key sent, sink holds no secrets, budget bounded.
Batch: same basename in two folders, existing output preserved, output never the source,
language tagged once, audio → m4a, symlink dedupe, 250-job output discovery, cheap output
index, resume skips a QC-passing result. Events: off by default, describe a job, no secrets,
unwritable log does not break the run.

## 6. Actual execution (local, this machine)

```
301 passed  (fast group: jobs, reliability, retry, config, cache, workspace, providers,
             security, fit, timeline, srt, reference docs, docs integrity)
 28 passed  (pipeline, run in three parts to stay inside a 300 s step)
 43 passed  (server, limits/batch/capabilities)
 37 passed  (concurrency, demo gate, CLI)
420 total (37 documentation integrity, 6 the verification harness, 5 the benchmark)
ruff check  — clean      ruff format --check — clean
python scripts/gen_reference.py --check — current
python -m dubforge doctor — ok
python scripts/ci_demo_check.py — passed (expected QC failure verified)
python -m build + twine check — wheel and sdist PASSED
```

## 7. Cross-platform

CI runs Linux 3.10/3.11/3.12, macOS 3.12 and Windows 3.12, plus the Windows-only workspace
step. The platform difference in recovery is *tested on both*: pid liveness on POSIX, a
heartbeat where pids cannot be probed. Results are recorded in the CI run for this commit,
not asserted here in advance.

## 8. Performance

Measured with `scripts/benchmark.py --sizes small` on this machine (stub providers, so this
is engine overhead rather than vendor latency): 9.2 s of media, 1.3 MB, plan 67 ms, dub
5.3 s, job layer +0.04 s, peak RSS 34 MB, 15 files left in the work directory. The hot path
is unchanged: the pipeline does the same ffmpeg work. What was added is small
and bounded — one heartbeat thread per running job (one write every 15 s), one claim file
per job, and an output index that reads JSON without building job objects (250 records
resolved in milliseconds, versus a full `Job` parse per record). The reference docs
generator and the test suite are the slowest parts of CI, not the product.

## 9. Backward compatibility

* No public name changed: 21 MCP tools, every CLI command, `pipeline.dub()`, `JobStore`,
  `JobRunner`.
* Old job documents load; two-part ids are marked `legacy_id` and simply cannot be retried
  (no request snapshot) — re-submitting them works.
* New `Settings` fields all have defaults: `job_heartbeat_stale_s`, `job_heartbeat_interval_s`,
  `qc_max_chars_per_line`, `qc_max_lines`, `qc_max_reading_cps`, `qc_min_display_s`.
* `JobStore.update()` keeps its signature; `token` is an optional keyword.
* Two QC checks can now **fail** a run that used to pass (`subtitle_timing`,
  `streams_present`). Both describe broken output, and both are documented in
  `docs/quality.md`.

## 10. Remaining risks

* Cancellation is still cooperative; a provider call in flight is not interrupted.
* A claim from a *different host* is judged by heartbeat age alone, so a badly skewed clock
  can delay (never cause) recovery.
* `JobStore.list()` still parses every job document; fine at thousands, not at hundreds of
  thousands.
* Network adapters remain `verified: false`. `live=true` health proves a credential, not a
  model.
* The `ambiguous` outcome cannot be resolved automatically — that is a deliberate limit, not
  an oversight.

## 11. Release readiness

Ready to tag `v0.2.1`. Manual steps unchanged from 0.2.0: the PyPI trusted publisher, and
the repository description/topics that this account's GitHub App cannot set.
