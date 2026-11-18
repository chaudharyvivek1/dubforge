# Changelog

All notable changes. Dates are ISO. This project follows semantic versioning.

## [0.9.2] — 2026-11-18

Documentation consistency (Phase 6 of the quality roadmap). One document contradicted another
about how many QC checks exist, and the test meant to catch that only read one page.

### Fixed — the QC inventory now comes from the source

* `docs/assessment.md` said "**15 checks**" in its implemented-inventory table while another
  section of the same file said twenty-three. The count is 23 on a normal run (24 when the
  source has more than one audio track), which the README and `docs/quality.md` already had
  right — the stale row was the defect, and the brief's "README vs assessment" framing was
  half the story: the contradiction was *inside* the assessment.
* `scripts/gen_reference.py` now generates **`docs/qc-inventory.md`** by parsing `qc.py` with
  `ast`: every `_check("<name>", …, severity=…)` call, deduplicated by name, labelled with the
  branch it sits in (`always`, `always (both branches)`, `only when <condition>`). It is gated
  by `--check` in CI like the other three generated pages, so it cannot drift from the code.
  Reading it out of the AST also settled a question the docs had been vague about: 26 call
  sites, 24 distinct checks, two of them emitted in two forms (`dub_is_audible`,
  `duration_preserved`), one input-dependent (`multiple_audio_tracks`).

### Fixed — the integrity tests cover every page

`tests/test_docs_integrity.py` used to read `docs/quality.md` by name, which is exactly why a
stale row in `docs/assessment.md` survived four releases. It now:

* discovers pages with `docs/*.md` rather than a list, so a new page is covered the moment it
  exists (`test_every_docs_page_is_scanned_by_these_tests` asserts the glob sees them all);
* compares `docs/quality.md`'s table to the generated inventory **in both directions**, names and
  severities, and requires input-dependent checks to be documented as conditional;
* scans every non-release page for a stated QC count and requires it to be the number the code
  produces — matching the phrase ("fifteen checks"), never a bare number;
* checks provider totals against the registry (subset claims like "a probe for 7 adapters" are
  left alone, because they are true);
* checks that every adapter named in `docs/providers.md` exists;
* checks that every `dubforge <command>` named in the docs is a real subcommand;
* checks that the committed `docs/verification.json` validates, carries a schema, and — while it
  is still empty — is exactly what the writer produces today;
* and asserts README/pyproject/CI agree on Python 3.10+ and on Linux/macOS/Windows.

Two regression tests reconstruct the original defect (a page saying "15 checks", and a phantom
table row) and require the checker to fail on it, so the fix is proven rather than asserted.

### Fixed — `docs/verification.json`

It was written by the 0.2.1 script: no `schema`, `dubforge_version: "0.2.1"`, zero rows.
Regenerated with the current writer (`verify.save`), so it now says schema 1 and 0.9.2, with the
same note and still **zero rows** — nothing was verified live and nothing was fabricated.

### Added

* `docs/architecture.md` documents the recovery window measured in Phase 1: a `SIGKILL`ed worker
  is reconciled to `interrupted` only once its heartbeat is older than 180 s, a live-but-silent
  worker is deliberately not taken over, and recovery does not increment attempt history.
* `docs/quality.md` points at the generated inventory as the authority and keeps the prose.

### Tests

668 → 681.

## [0.9.1] — 2026-11-18

Polling reliability, timeout correctness and error classification. 0.9.0's retry treated the
symptom of a Windows race; the cause was that three different failures all looked like "no job".

### Fixed — a timeout that was not the caller's

* `wait()` measured its deadline with `time.time()` and slept in fixed 100/250 ms steps, so a
  50 ms timeout could overrun and a 1 s timeout could nap past its own deadline. It now uses
  `time.monotonic()` and clamps every sleep to the remaining budget.
* **`timeout=0` reported an existing job as missing.** A finished job is now returned; a running
  one raises the timeout error, which is what the caller asked about.

### Fixed — three failures wearing one mask

`JobStore.get()` returned `None` for a missing record, an *unreadable* one and a *corrupt* one,
and the poll loop turned all three into `no job '<id>'`. That was two lies: a job that existed
was reported absent, and a corrupt record was retried as if it might heal.

* `JobError` gained a `kind` — `missing`, `unreadable`, `corrupt`, `job` (default, so existing
  callers are unaffected) — with the message unchanged for every existing case.
* `JobStore.get(..., strict=True)` classifies instead of hiding; `JobRunner.status()` uses it.
  The lenient default is kept for the callers that only want "the job or nothing".
* `wait()` retries `missing` **and** `unreadable`, bounded by the deadline — CI proved the second
  is needed: on Windows the window inside `os.replace` surfaces as `Permission denied`, so a
  policy that treated it as fatal would abort a healthy poll. Whichever is still true at the
  deadline is raised with its own message, so neither is disguised as the other.
* A `corrupt` record or any other `JobError` propagates on the first read: waiting cannot heal a
  bad document. Unexpected exceptions always propagated and still do.
* The transient-miss budget is now the deadline, not a fixed 40-attempt counter, so retries can
  never extend a caller's wait.
* Every `no job` raise in the module is classified — five sites, not just the polling path.

### Investigated, not changed — is `os.replace` racy?

A stress test (a writer replacing the record 200 times while a reader reads) never observes a
half-written record on Linux: `os.replace` is atomic on POSIX, and the temp file plus rename in
`save()` is the right pattern. On Windows the same test reproduces the 0.9.0 failure directly —
`Permission denied` during a replace — which confirms the mechanism: a *sharing violation*
(an `OSError`, previously flattened to `None`), not a missing-file window. No lock was added,
because the evidence does not support one; what was missing was the classification that lets the
poll loop tell "absent or busy for a moment" from "not a job, ever".

### Tests

647 → 668. New `tests/test_wait_deadline.py` (19) covering the brief's A–H cases: deadline
respected for a nonexistent job (with elapsed time measured on a monotonic clock), very short
timeouts, `timeout=0`, transient-miss recovery, persistent misses under a timeout, a deadline that
is not reset by retries, completion/failure/cancellation during polling, corrupt and unreadable
records propagating immediately, unexpected errors propagating, `JobError` keeping its old shape,
and a concurrent-writer stress test for registry atomicity.

## [0.9.0] — 2026-11-18

The ledger becomes authoritative. 0.8.0 wrote `docs/verification.json`; nothing read it. Now the
record decides what is verified, a row expires, and a claim with no evidence is reported as a
contradiction instead of believed.

### Fixed — the ledger was write-only

* `spec.verified` was a static field, so a record full of `ok` rows changed nothing:
  `capabilities`, `doctor`, the provider list and the pipeline's "unverified adapter" warning all
  read the declaration. They now read `verify.state_for(...)`, which resolves the record and
  reports where the answer came from (`record`, `local`, `declared`, `none`).
* The pipeline warning is now `_verification_note()`, a testable function: it is silent for a
  proven adapter and names the reason for an unproven one.

### Added — expiry, and the local/network distinction

* A verification is a fact with a date. An `ok` row older than `MAX_AGE_DAYS` (180) stops
  counting, is reported as `stale` with its age, and says "re-run to renew" — vendors change
  endpoints, models and prices, so a row from another season is evidence of another system.
* `Spec.local` makes an existing distinction explicit: local adapters (the stub, the transcript
  reader, ffmpeg centre-removal, the two "none" providers) run in-process or in ffmpeg, so the
  test suite proves them on every run. Their declaration *is* evidence, labelled `source: local`
  — a different promise from "verified against a live API on 2026-11-18".
* A network adapter that declares `verified` with no row behind it is now a reported
  **contradiction**. Turning the resolver on found one immediately: `ffmpeg_center` declared
  verified and was neither local nor recorded — the model had no way to say what it meant.

### Changed

* `verify.status()` reports `verified` (live rows only), `verified_live`, `local`, `stale`,
  `contradictions`, `max_age_days` and per-adapter `states`.
* `capabilities` provider rows carry `verified` (effective) plus a `verification` object with the
  date, age, source and note. `doctor` reports stale and contradiction counts.

### Fixed — a poll loop that turned a hiccup into a missing job

* `JobRunner.wait()` called `status()` in a loop and let any `JobError` through. The registry
  record is written with `os.replace`, and on Windows a concurrent reader can observe the moment
  where the path does not resolve — so a batch failed with `no job 'b-e1b8a1dc'` while the job
  was running perfectly well. `wait()` now tolerates a transient miss, and still raises the
  precise `no job` error when a job never resolves at all (a timeout message would be worse).
  Found by CI on Windows, not by a test; the two tests that now cover it were written after.

### Tests

629 → 647. New `tests/test_verification_state.py` (15): a fresh row verifies, an old row expires,
the expiry is configurable, a failed row is not verified, an `ok` row with no readable date is not
trusted, no row means unverified, a local adapter is proven by the suite, a network declaration
without a row is a contradiction, the real registry has none, `capabilities` reports the ledger
and expires a stale row, and the pipeline warns only about unproven adapters; `tests/test_jobs.py` gained the transient-miss
and never-existed cases for `wait()`.

### Honest state

`docs/verification.json` still has zero rows: every network adapter is unverified, and every run
says so with its reason. What changed is that the mechanism now *works* — a dated row would flip
`capabilities`, silence the pipeline warning, and expire on its own.

## [0.8.0] — 2026-11-18

The verification ledger. The one thing left on the honesty ledger — no adapter has been
exercised against a live API — is the one thing that needs keys this repository does not have.
So this release makes it executable, auditable, and importable, without pretending any of it
has happened.

### Added — `dubforge verify` (P1)

* `verify.py` (moved out of `scripts/verify_providers.py`, which now delegates) with four
  actions:
  * **plan** — every network adapter, the environment variable it needs, whether it is present,
    whether a check exists for its stage, and what the probe costs in the vendor's units. It
    builds each adapter to ask `missing()` and calls nothing else; a spy in the tests raises if
    it ever touches `transcribe`, `translate`, `synthesize` or `voices`.
  * **run** — the smallest real call per stage, refusing without `--yes-live`. A missing key is
    `skipped`, a vendor error is `failed` with its `error_kind`.
  * **import** — accept a record produced on a machine that had keys, after validating it.
  * **status** — what the ledger says now.
* `docs/verification.md`: the procedure end to end, what a green row does and does not mean,
  and the honest state today.
* `dubforge_verification_status` (27 tools): read-only, so an agent can answer "is this provider
  verified?" from the record instead of from optimism.

### Added — import validation

An imported record is checked before it can influence anything: schema version, every
`stage/provider` pair must exist in the registry, no duplicate adapters, statuses from the fixed
set, an **ISO date on every `ok` row** (a claim without a date cannot be audited), and a
credential tripwire (`sk-…`, `api_key=…`, any 32+ character opaque string). Every problem is
reported at once, so one round trip fixes them all.

### Changed

* `doctor` reports the ledger in text and in `--json` (`verification`), so "is it verified"
  lives beside everything else rather than in a separate command.
* `capabilities` unchanged in shape; the ledger is a separate, explicit read.

### Tests

608 → 629. `tests/test_verify_providers.py` was rewritten against the module rather than the
script — same assertions, moved with the code — plus the ledger rules: plan readiness, plan
calls nothing, opt-in refusal, no key in a written record, unknown adapter, duplicate row,
unknown status, `ok` without a date, a credential in a field, dry-run import, and status counts.

### Honest state

`docs/verification.json` still has zero rows. Every adapter is unverified, calibration has no
measured samples, and every run reports sentence-level timing only. This release makes the next
step a command instead of a task.

## [0.7.0] — 2026-11-18

The last two passes, and a command that explains the bill. A regeneration that changes nothing
now reuses every stage: 6.2 s → 2.3 s on the demo, byte-identical output.

### Added — the decoded audio is cached (P1)

* `mix.extract_key()` + the stage cache: the two decode passes (16 kHz mono for recognition,
  48 kHz stereo for the bed) are keyed on the source's content hash and the format, so a
  re-run copies them instead of decoding the film again.
* `extract_mode` reports `reused` / `partial` / `rebuilt` per run, and `rebuilt_stages` gained
  `extraction` when it ran.

### Added — the stretched render is cached (P1)

* The segment store now keeps the render *after* its window-specific atempo stretch
  (`<seg_id>.placed.wav`), keyed on the speed actually applied and checksummed like the raw
  render. A re-run whose windows are unchanged skips one ffmpeg invocation **per line** —
  on a 300-line film, 300 processes.
* The measured duration is stored with the artifact, so reusing it costs no `ffprobe` call
  either. `placed_reused` reports how many lines came from it.

### Added — `dubforge explain` (P1)

* `explain.py`, `dubforge explain <job-id>`, and the MCP tool `dubforge_explain_job` (26
  tools). It reads `review.json` and prints: which stages were reused, which were rebuilt,
  where the time went (per stage, with shares), the QC failures, and what one more edited line
  would cost.
* It refuses to invent numbers: a job with no `review.json` gets an explanation of why, not a
  table of zeroes. The "if you edit one more line" table lives in code (`explain.NEXT_EDIT`)
  so the command and the documentation cannot drift.

### Decided against — per-region loudness and a spliced mix

The 0.6.0 note proposed reusing the mix outside edited windows. Measured before building it:
the mix stage is two passes over the audio, and a mix splice would need a loudness measurement
pass plus a splice pass — so it saves at most one pass while adding a global-gain correctness
problem (the gain is a function of the whole track). Extraction and placement, by contrast,
were two full decodes and one process per line. The cheaper thing to fix was the expensive
thing, so that is what shipped. The reasoning is in `docs/architecture.md` under "What a
stage costs", with the numbers that decided it.

### Tests

593 → 608. New `tests/test_explain_and_caches.py` (14): extraction key coverage, a second run
that must not call `extract_audio`, a corrupted extract, a changed source, placed-render reuse
with a counted `stretch`, invalidation when the window changes, the stored duration, and
`explain` against a finished job, a no-op job, a job without a review, and an unknown job.

### Benchmark (`--regen`, small clip, stub providers)

| case | TTS | separations | reused renders | bed | dialogue | mix | mux | wall |
|---|---|---|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | assembled | rebuilt | rebuilt | 6.2 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | reused | reused | reused | 2.3 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | spliced | rebuilt | rebuilt | 4.3 s |

### Known limitations

* A spliced dialogue still rebuilds the mix, and the analysis above says that is the right
  trade at this stage size.
* Word-level alignment remains provider-dependent and unverified live; calibration has no
  measured samples.
* The wall clock of a no-op run is now dominated by process startup and bookkeeping rather
  than by media work, which is a good place to be but not zero.

## [0.6.0] — 2026-11-18

Stage-level reuse and honest timing. A regeneration now reuses every stage whose inputs are
unchanged — renders, bed, dialogue track, mix, mux — and a run with no changes produces a
byte-identical file in a fraction of the time.

### Added — the mix and mux stages are cached (P1)

* `mix.py` keys each stage over exactly the inputs that determine it:
  * **mix** — the dialogue track's content hash, the bed's hash, the voice and bed gains, the
    ducking switch, the loudness target, the true-peak ceiling, the runtime, the sample rate;
  * **mux** — the mix's hash, the source's hash, the audio bitrate, whether the source has
    video.
* An exact hit skips `loudnorm`, `duck_and_mix` and `mux` entirely: an unchanged run of the
  9-second demo went from 6.6 s to 2.9 s and produced **byte-identical** output.
* The cache is content-addressed, which is the honest part: with stub providers two different
  texts can render identical audio, so an edited line can still reuse the mix. The columns
  describe bytes, not intentions, and the benchmark says so.
* **The mix is never reused for a spliced dialogue.** Loudness normalisation applies one gain
  to the whole track, so a changed line moves that gain and every untouched region with it.
  A splice therefore misses the mix key and rebuilds — `mix_mode: "rebuilt"` in the report.
* Artifacts are verified by SHA-256; a truncated or replaced file is a miss. `StageCache`
  carries `stats()` and `cleanup(max_bytes=…, dry_run=…)`, largest-first.

### Added — per-stage timing (P1)

* `mix.summarise_timings()` produces `work.stage_seconds`, `work.total_stage_seconds`,
  `work.slowest_stage` and `work.slowest_seconds` in `review.json`, so a user can see whether a
  regeneration is paying for synthesis, separation or the mux. Zero-second steps (reuse
  notices) are excluded — they are not measurements.

### Fixed

* `work.rebuilt_stages` is now derived from what actually ran (`_rebuilt_stages`) instead of a
  hardcoded list. 0.4.0's list claimed the dialogue track was rebuilt even when untouched;
  0.5.0 fixed that one case by hand, which was the same mistake waiting to happen again.
* `capabilities` no longer advertises an `always_rebuilt` list that was about to become false;
  it publishes `segment_reuse`, `stem_cache`, `dialogue_reuse`, `stage_cache` and
  `always_runs` (`qc`, `report`), which are true by construction.
* A reused mix left `normalised` undefined for the QC call; the dialogue-alone path is now
  resolved explicitly, and the QC audio checks still measure the normalised dialogue.

### New settings

`stage_cache` (true). `DubResult` gained `mix_mode` and `mux_mode`.

### Tests

579 → 593. New `tests/test_stage_cache.py` (13): key coverage in both directions (content, not
names), truncated artifacts, cleanup order, timing summary, and pipeline tests that monkeypatch
`loudnorm`/`duck_and_mix`/`mux`/`assemble_dub` and assert a no-op regeneration calls **none** of
them while still producing identical bytes.

### Benchmark (`--regen`, small clip, stub providers)

| case | TTS | separations | reused renders | bed | dialogue | mix | mux | wall |
|---|---|---|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | assembled | rebuilt | rebuilt | 6.6 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | reused | reused | reused | 2.9 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | spliced | rebuilt | rebuilt | 5.2 s |

### Known limitations

* A spliced dialogue always rebuilds the mix: the normalisation gain is global, and splicing
  the mix would misrepresent the audio. Reusing the mix for untouched regions would need
  per-region gains, which the loudness model here does not have.
* Overlapping dialogue still costs a full assembly.
* Word-level alignment remains provider-dependent and unverified live; calibration has no
  measured samples.

## [0.5.0] — 2026-11-17

Partial remixing. Editing five lines of a 300-line film no longer rebuilds the dialogue
track: the changed lines are spliced into the previous one in a single pass.

### Added — partial remixing (P1, the 0.4.0 "next milestone")

* `dialogue.py` keeps the assembled dialogue track with a per-line manifest (`seg_id`, start,
  rendered duration, speed, clip SHA-256) and a SHA-256 of the track itself.
* On a regeneration, only the changed lines are replaced: their **own** time ranges are muted
  and the new renders are mixed in at the same starts, in one ffmpeg pass whose cost does not
  grow with the length of the film.
* Only a line's own extent `[start, start + rendered_duration]` is muted, so a neighbour's
  audio in an overlapping window is never cut. **If a changed line overlaps another line the
  splice is refused** and the run falls back to a full assembly — zeroing a shared range would
  delete the other speaker.
* An unchanged run reuses the track outright (`dialogue_mode: "reused"`); a changed one
  splices (`"spliced"`); anything else assembles (`"assembled"`). `review.json` records which,
  and `work.rebuilt_stages` no longer lists the dialogue track when it was not rebuilt.
* Splicing reuses *placement*, not processing: normalisation, ducking, the mix and the mux
  still run, because they depend on the current dialogue.

### Fixed

* The `work.rebuilt_stages` claim from 0.4.0 was too coarse: it listed the dialogue track as
  rebuilt even when the track was untouched. It is now derived from what actually ran.

### Tests

566 → 579. New `tests/test_dialogue_splice.py` (12): the spliced track is compared
sample-by-sample against a from-scratch rebuild and must match within 1%, overlap refusal,
track integrity (truncated track is a miss), reuse and splice paths through the pipeline.

### Benchmark

`--regen` now reports the dialogue mode beside the counts. On the small clip: full run
assembled, 0 edits reused, 1 edit spliced, all lines edited assembled.

### Known limitations

* A splice is refused whenever a changed line overlaps another, which is correct but means
  overlapping dialogue always costs a full assembly.
* Word-level alignment is still provider-dependent and unverified live.
* Calibration still has no measured samples.

## [0.4.0] — 2026-11-20

Incremental processing, honest job identity and a working human review loop. Three confirmed
identity defects, one concurrency defect, and a cache that makes regeneration cheap.

### Fixed — job identity (P0)

* **`source_fingerprint()` was `size:mtime_ns`.** Two different videos of equal size with
  equal timestamps — a copy, or a re-encode that preserved the size — were the *same job*.
  It is now a streamed SHA-256 plus the size, memoised per `(path, size, mtime)` so a submit
  hashes once and every later call in the run is free. Content is the identity: a rename keeps
  the job, a byte change does not.
* **`retranslate` was missing from `REQUEST_FIELDS`.** Two runs that produce different
  translations shared one identity. `retranslate`, `lipsync` and `separator` are now part of
  it; limits, logging and paths deliberately are not.
* **A supplied transcript was identified by its path.** Editing the file in place kept the
  same job. The transcript's bytes are now hashed into the request identity, so an in-place
  edit is a new request and a copy under another name is the same one.

### Fixed — segment concurrency (P1)

* **The segment lock was taken after synthesis**, so two workers that both missed the cache
  both called a paid provider; the lock only decided who wrote last. The order is now
  claim → re-check the cache under the claim → synthesise → save → release.
* Locks carry an owner token and are stolen only when the owner is **gone** (dead pid), never
  merely because they are old — an age-first check would let a second worker rob a slow but
  live renderer, which is the same duplicate spend in a different disguise.
* Waiting is bounded (`segment_lock_wait_s`, 30 s). A worker that cannot get the lock renders
  anyway and says so: slower, never wrong. Cross-process tests use `spawn` and count writes.

### Added — source-stem cache (P1)

* `stems.py` caches the separated background bed, keyed on the source content hash, the
  separator, its model, `keep_background` and the sample rate. Regeneration no longer repeats
  separation: in the benchmark a re-run reuses the bed and all unchanged renders.
* Integrity: SHA-256 per bed, atomic writes, an exclusive claim while separating, corrupted or
  empty beds are misses, and `dubforge cache info|clean` reports and bounds the cache (dry run
  by default).
* **The final mix is never cached.** Its ducking envelope depends on the current dialogue, so
  `review.json` lists `dialogue track`, `normalisation`, `ducking`, `mix` and `mux` as
  rebuilt on every run, whatever the reuse ratio says.

### Added — review workflow (P1)

* `review.py` + `dubforge review export|validate|apply` + three MCP tools: export one editable
  row per line, validate against the job, apply only the changed lines.
* Validation refuses an unknown schema, a stale file (source fingerprint mismatch), unknown or
  duplicated segments, non-string or oversized text, and any attempt to set paths, tokens or
  provider keys. Applying twice is a no-op the second time.
* A static `review.html` is written beside `review.json`: no server, no remote assets, changed
  rows highlighted, alignment precision stated per line.

### Added — output safety (P1)

* The mux now writes a `.partial` file, validates it (readable, has audio, kept its video,
  duration within 5 s of the source), and only then moves it into place. A failed mux deletes
  the partial and leaves the previous output untouched.
* Overwriting is opt-in (`--overwrite` / `overwrite_output`). An existing output is refused
  with an explanation rather than silently replaced.

### Added — work accounting (P1)

* `review.json` gained a `work` block: segments, reused renders, rendered segments, stems
  reused and rendered, cache hit ratio, the stages that were rebuilt, and per-stage seconds.
  A run that reuses 295 of 300 renders still rebuilds the dialogue track and the mix, and the
  report says so.

### Changed

* `Job` records the source fingerprint and the review path; `Job.summary()` exposes both.
* New settings: `segment_lock_wait_s`, `stem_cache`, `stem_cache_max_bytes`, `overwrite_output`.
* New CLI: `dubforge review export|validate|apply`, `dubforge cache info|clean`,
  `--overwrite` on `dub` and `regen`. New MCP tools: 25 total.

### Documentation

* `ROADMAP.md` and `docs/assessment.md` corrected: both described 0.2.1 as current and listed
  features as missing that 0.2.x and 0.3.0 shipped (per-segment regeneration, subtitle
  line-length and reading-speed validation, review artifacts).

### Tests

499 → 566. New: identity (17), segment concurrency across processes (12), stems (14), review
workflow (17), output safety (6), benchmark regeneration comparison (1).

### Known limitations

* Remixing is still whole-track: unchanged renders and the bed are reused, but the dialogue
  track, normalisation, ducking and mux are rebuilt. Partial remixing is the next milestone.
* Word-level alignment remains provider-dependent and unverified live; every run says
  "sentence-level timing only".
* Calibration still has no measured samples.
* Hashing the source costs one read of the file per job; on a 2 GB film that is a second or
  two on a local disk, and it is the price of not confusing two videos.

## [0.3.0] — 2026-11-19

A quality release: the timing model stops colliding lines, alignment is measured or absent
and never invented, one edited sentence costs one synthesis, and the QC report looks inside
a rendered line as well as at the mix.

### Fixed — timeline (P0)

* **A window could exceed the room before the next line.** `windows()` floored every window at
  `min(duration × max_stretch, max(duration, 0.4))`, so a 0.3 s segment followed 0.35 s later
  received a 0.4 s window and collided by 0.05 s. The floor is gone: a window is the room
  available, and a squeezed line is *reported* (`window_tight`) instead of allowed to overrun.
* **Overlapping dialogue is preserved.** When the next line starts before this one ends — two
  speakers at once, or a bad ASR end timestamp — the window is the line's own duration.
  Serialising an overlap would cut one speaker off to let the other start.
* The rules for the seven cases in the brief (overlap, simultaneous speakers, short silence,
  bad end timestamps, very short lines, the last line, differing target speeds) are now
  deterministic tests in `tests/test_timeline_alignment.py`.

### Added — word alignment (P0)

* `alignment.py` stores **measured** target word timings when a provider reports them
  (`TTSProvider.synthesize_with_timestamps`, default: none), validates ordering, duration and
  containment, and rejects bad timings rather than shipping them.
* There is deliberately **no estimate state**: no helper distributes a sentence's duration
  across its words, and a test greps the module's code for exactly that. A run with no
  measured alignment says "sentence-level timing only" in its warnings, and `review.json`
  marks every line `sentence_level_only: true`.

### Added — speech-rate calibration (P1)

* `calibration.py` + `scripts/calibrate.py`: a version-controlled dataset
  (`data/calibration/samples.jsonl`), provenance on every sample (provider, voice, model,
  method, counting convention, date), distributions rather than averages (median, p10, p90,
  n), and an override gate of **30** samples per language.
* The dataset **ships empty**. Every language therefore reports `estimate`, `fit.source_for()`
  says so, and `docs/calibration.md` is generated by the same script. `--measure` renders
  fixed sentences through a real provider and refuses to run without `--yes-live`.

### Added — selective regeneration (P1)

* `segments.py`: a per-segment store of the **raw** render plus a fingerprint of everything
  that changes it (target text, voice, provider, model, language, style, emotion, sample
  rate). Timing is excluded on purpose — the stretch is re-applied every run because it
  depends on the window, and it is free.
* `pipeline.regenerate()` + `dubforge regen` + `dubforge_regenerate_segments`: edit lines by
  segment id or index, re-render only those, and write to a **new** file. A previous output
  is never replaced by a run that might fail.
* Artifacts are only reused when provably intact: the record carries the audio's SHA-256, a
  truncated file is a miss, writes are atomic, a per-segment lock serialises concurrent
  workers, and a workspace with no segment store simply renders everything (0.2.1
  compatibility).
* **Fixed:** `--transcript` was documented and silently ignored — the pipeline always ran
  recognition. It now loads a transcript JSON or SRT.

### Added — QC and review (P1)

* Three per-line checks (23 total): `short_tts_output` (a render far shorter than its text
  implies), `excessive_compression` (atempo above 1.10×, audible even when legal) and
  `silence_inside_line` (dead air inside a render, sampled to bound cost).
* `review.json`: one stable row per line with source text, translation, speaker, both
  timings, voice, audio path, fit verdict, alignment precision, whether it was regenerated,
  and its warnings — the artifact a review tool should read.
* QC checks now carry `declared` (the class: `fail`/`warn`) alongside `severity` (this run's
  outcome), so a passing warning and a passing blocker are distinguishable.

### Fixed — documentation (P0)

* `docs/quality.md` said fifteen checks, listed `multiple_audio_tracks` twice and did not mark
  it conditional; `ROADMAP.md` carried stale counts; the README described job directories by
  source path when jobs are request-addressed. All corrected.
* The documentation test now compares **check names and severities** in both directions
  instead of searching for a number, and asserts each check appears once.

### New settings

`segment_reuse` (true), `retranslate` (true), `qc_min_tts_ratio` (0.3),
`qc_max_audible_speed` (1.10), `qc_line_silence_ratio` (0.5), `qc_silence_sample` (40).

### Tests

418 → 499. New: timeline rules (9), alignment (9), calibration (19), segments and
regeneration (27), review artifacts and per-line checks (10), documentation consistency (3).

### Known limitations

* Word-level alignment is only as good as the provider that reports it; none of the
  adapters here is verified live, so every run is sentence-level today and says so.
* Calibration has no measured samples yet; the estimates in `fit.CPS` are unchanged and
  labelled.
* Regeneration re-runs the whole pipeline around the reused lines (ASR is skipped, the mix and
  mux are rebuilt), because partial remixing is a bigger change than this release carries.
* Cancellation remains cooperative.

## [0.2.1] — 2026-11-18

A reliability release. Same product, harder to break and harder to overpay: no public name,
CLI command or MCP tool changed.

### Fixed — job liveness (P0)

* **Touching a job with a live pid now records a heartbeat.** A record carrying a pid but no
  heartbeat (every 0.2.0 document, and any caller that sets only `pid`) was indistinguishable
  from a crashed one where pids cannot be probed — the Windows path — so a running job could
  be declared interrupted. A claim is likewise judged by pid only where pids can be probed.

* **A live job was reported `interrupted` while its provider call was still running.** The
  recovery check treated a stale heartbeat as death, so a four-minute synthesis could be
  killed by a status request. Recovery now prefers the pid where a pid means something
  (POSIX) and only falls back to the heartbeat where it does not (Windows, another host).
  Workers also refresh the heartbeat on a timer, not only between stages.
* **A queued job with no claim was read as dead.** An unclaimed job is waiting, not lost.
* **Clock skew** between hosts no longer looks like death: a heartbeat from the future
  counts as fresh.

### Fixed — job identity (P0)

* **One source could only have one job.** The id was `<stem>-<pathhash>`, so Hindi and Tamil
  for the same video collided and the second submit returned the first job. The id is now
  `<stem>-<pathhash>-<requesthash>`, where the request hash covers everything that changes
  the result — languages, providers, models, voices, glossary, context, style, emotion, mix
  and timing settings — and deliberately excludes `workdir`, `cache` and logging.
* Identical requests still deduplicate (`duplicate: true`), and different provider
  configurations can never inherit each other's output.
* Two-part ids from 0.2.0 still load and are marked `legacy_id`.

### Fixed — ownership (P0)

* **Two runners, or two processes, could run the same request.** Ownership was an in-memory
  set. It is now a claim file created with `O_EXCL` holding a token, pid, host and start
  time: one winner, no check-then-write race. `update(token=…)` refuses a worker that lost
  the claim, `heartbeat()` and `release()` are owner-only, a dead claim is reclaimed and a
  live one is never stolen. Verified with a real `multiprocessing` child process.

### Fixed — batch outputs (P0)

* **Two folders with the same filename wrote to one output.** `class-a/lesson.mp4` and
  `class-b/lesson.mp4` produced `out/lesson.hi.mp4` twice. Outputs now preserve the relative
  folder structure, and a genuine collision appends a deterministic 6-character hash of the
  resolved source path — so re-running a batch lands on the same names instead of
  accumulating copies.
* An existing output is never replaced; the source is never an output; symlinked duplicates
  are dubbed once; files inside the work directory are never inputs.
* **Output discovery no longer stops at 200 jobs**, which could have let a large batch
  re-dub its own results.

### Fixed — retries and money (P1)

* **A non-idempotent POST could be retried after a timeout**, which is how one line becomes
  two charges. Retries are now operation-aware: a 429/5xx *answer* is retried (nothing was
  billed), a network failure is retried only when the call is idempotent or carries a
  vendor-documented idempotency key — otherwise it raises `kind="ambiguous"` and says the
  request may have been accepted and billed.
* OpenAI calls pass its documented `Idempotency-Key`. No header is invented for any other
  provider, and no call is marked idempotent on a guess.
* Retries are counted: `provider_retries` in the job, a warning in the run, a section in
  `report.md`, and a `provider.retry` event.

### Fixed — lifecycle and results (P1)

* **Illegal transitions are refused.** `TRANSITIONS` is enforced by the store, so a terminal
  job cannot quietly become active again; the only way back is `retry`.
* **A retry no longer shows the previous attempt's results.** `output`, `audio`,
  `subtitles`, `qc`, `warnings` and `providers_used` are cleared, `attempts` increments and
  `attempt_history` records the attempt (bounded to 50 entries).
* **A retry re-runs the original request**, from the snapshot stored with the job, not the
  current settings.
* `qc_failed` distinguishes "finished and failed QC" from "succeeded" without inferring it
  from two fields.

### Fixed — output and files (P1)

* An explicit `--out` into a folder that does not exist now creates it, instead of failing at
  the mux after the whole run was paid for.
* Cleanup still refuses anything outside `<workdir>/jobs/`, and now also removes the claim
  file.

### Added — QC (15 → 20 checks, 21 for a multi-track source)

`subtitle_timing` (fail: invalid durations, overlaps), `subtitle_readability` (warn: line
length, reading speed, minimum display time — all configurable), `streams_present` (fail:
dub audio and picture both present, one audio stream), `video_codec_preserved` (warn),
`audio_layout` (warn: sample rate and channel count), `not_mostly_silent` (warn: more than
75% silence in the dub track).

### Added — observability

`DUBFORGE_EVENTS=<file>` writes append-only JSONL: `job.created`, `job.claimed`,
`job.stage`, `job.completed`, `job.failed`, `job.cancelled`, `job.retry`, `job.recovered`,
`provider.retry`, `provider.fallback`, `qc.result`, `cleanup`. Stable names, timestamps,
job and attempt identifiers, provider names, error kinds — and no key, header or transcript
ever. Off by default; an unwritable log never breaks the run.

### Added — live verification harness

* `scripts/verify_providers.py` is the process behind `verified: false`: for every adapter
  with a key it makes the smallest real request that proves something (two seconds of audio,
  one sentence, one phrase), checks the shape of the answer, and records the outcome in
  `docs/verification.json` with the date and API docs link. Never runs in CI, never prints or
  stores a key, treats a missing key as `skipped` rather than `failed`, and exits non-zero on
  a real failure.
* A test now ties `spec.verified` to that record: an adapter claiming verification without
  an `ok` row fails the build.

### Added — a benchmark

* `scripts/benchmark.py` measures real media at three sizes (≈9 s, 55 s, 184 s) with stub
  providers: planning time, run time, the job layer's overhead, peak RSS and the files a
  finished job leaves behind. Not in CI (a shared runner's timing is not a measurement) and
  vendor-free (a vendor benchmark ages in a week).

### Added — documentation integrity

* `tests/test_docs_integrity.py` checks the hand-written docs the way
  `test_reference_docs.py` checks the generated ones: every relative link resolves, the
  version agrees across `pyproject.toml`/`__init__.py`/`CITATION.cff`, the changelog has an
  entry for the current version, `SKILL.md` lists exactly the reference files that exist,
  every page in `docs/` is linked from the README, no doc names an MCP tool that does not
  exist (unless it is explaining the absence), and the QC check count in the README matches
  the number the pipeline actually produces.

### Added — skill

Four references for agents: `jobs.md` (identity, claims, attempts, heartbeat), `batch.md`
(collisions, resume, a school/studio workflow), `reliability.md` (retries, idempotency,
health, fallback, events), plus an updated `SKILL.md` whose third rule is "a retry can cost
money".

### New settings

`job_heartbeat_stale_s` (180), `job_heartbeat_interval_s` (15), `qc_max_chars_per_line` (42),
`qc_max_lines` (2), `qc_max_reading_cps` (20), `qc_min_display_s` (0.8).

### Tests

316 → 420. New: heartbeat and recovery (8), identity (7), claims including a real
subprocess (7), attempts and transitions (7), runner behaviour (4), retry safety (8), batch
collisions and scale (10), events (3), QC additions (3).

## [0.2.0] — 2026-11-17

The job engine release. An agent can now submit work, watch it, cancel it, retry it and
collect the result — without a database, a queue server or a daemon. Every existing CLI
command, Python API and MCP tool keeps its name and behaviour.

### Added — job engine (`jobs.py`)

* **Persistent jobs.** One JSON document per job in `<workdir>/registry/<job-id>.json`,
  written atomically. The id is derived from the source *path*, so submitting the same
  file twice returns the same job instead of starting a second dub.
* **Lifecycle.** `queued → validating → processing → quality_check → completed`, with
  `failed`, `cancelled` and `interrupted` as terminal states. `interrupted` is derived:
  a job whose pid is gone (or whose heartbeat is stale) is reported as interrupted and
  **never** as completed.
* **Recovery.** `JobStore.recover()` reconciles every job on disk at startup; a job left
  by a killed process is marked interrupted with a reason, not silently retried.
* **Background execution.** `JobRunner` is a bounded thread pool (`max_concurrent_jobs`),
  not a queue server. Duplicate submission of a running job is refused; a finished job
  re-submitted is a new attempt.
* **Cooperative cancellation.** `pipeline.dub(cancel=…)` checks between stages and
  before every paid call. Honest limits, documented: a line being synthesised finishes,
  and an ffmpeg run in progress is not killed.
* **Progress.** `pipeline.dub(progress=…)` reports stage and step counts, which is what
  the job document shows.

### Added — MCP tools (10 → 21)

`dubforge_submit_job`, `dubforge_get_job_status`, `dubforge_get_job_result`,
`dubforge_list_jobs` (paginated), `dubforge_cancel_job`, `dubforge_retry_job`,
`dubforge_cleanup_jobs` (dry run by default), `dubforge_get_capabilities`,
`dubforge_validate_config`, `dubforge_check_provider_health`,
`dubforge_estimate_cost`. `dubforge_analyze_video` was **not** added: `probe_media` plus
`plan_dub` already answer it, and a duplicate tool is a worse contract than a missing one.

### Added — intelligence

* **`capabilities.py`** — one derived answer to "what can this install do": stages,
  providers with readiness and verification, features (diarisation, cloning, separation,
  lip sync, duration-aware translation), formats, optional dependencies and limits.
  `doctor`, `providers`, the CLI and the MCP tool all read it, so they cannot disagree.
  A stub provider never makes a feature "supported".
* **`limits.py`** — refusals, not warnings: `max_input_bytes`, `max_input_seconds`,
  `max_run_seconds`, `max_estimated_usd`, `max_concurrent_jobs`, `max_retries`,
  `job_retention_days`. Checked in `plan()` before the first paid call, and between
  stages while running. Each failure names the setting to change.
* **`batch.py`** — a folder of files through the same pipeline: failure isolation,
  resume (a completed, QC-passing output is skipped), outputs written to
  `<workdir>/out/`, aggregate estimate, and no source-file overwrite.

### Added — provider reliability

* **Failure classification.** Every HTTP failure is `auth`, `rate_limit`, `timeout`,
  `network`, `bad_request`, `not_found`, `server` or `unknown`, with a `retryable` flag.
  `Retry-After` is honoured instead of a guessed backoff.
* **Health checks.** `Provider.health(live=True)` makes one cheap authenticated call for
  the adapters that have a free endpoint (OpenAI, ElevenLabs, Groq, Deepgram, DeepL,
  Ollama, edge-tts) and says "no cheap probe" for the rest. `live=False` never touches
  the network.
* **Opt-in fallback.** `--fallback` / `DUBFORGE_FALLBACK` walks the provider chain when a
  stage fails. It is announced in the warnings ("different quality and cost"), the voices
  are re-mapped if the voice provider changed, and the transcript is not cached under a
  provider that did not produce it.
* **Actual providers recorded.** `providers_used` is written to the job document, the
  manifest and `report.md`, separately from the providers that were requested.

### Added — CLI

`dubforge capabilities`, `dubforge validate-config`, `dubforge jobs list|show|cancel|
retry|cleanup` (cleanup is a dry run unless `--yes`), `dubforge batch <folder|file>`.
All of them support `--json`.

### Changed behaviour

* `dubforge doctor` now prints features and limits, from the same source as
  `capabilities`.
* A job document lives beside the workspace manifest; the manifest keeps its own job.
* New settings (all optional, all validated): the seven limits above plus `fallback`.

### Tests

235 → 311. New: lifecycle transitions, atomic job writes, restart recovery, interrupted
vs failed, cancellation of queued and running jobs, retry budget, pagination, cleanup
boundaries, duplicate submission, every limit (bytes, duration, runtime, estimate), limit
overrides from the environment, capabilities derived from the registry, a stub not making
a feature supported, doctor/capabilities agreement, batch failure isolation and resume,
output paths outside the input folder, cancellation mid-pipeline, progress reporting,
opt-in fallback with an announcement, provider classification, health without a network
call, and the eleven new MCP tools including a submit → status → result round trip.

### Documentation

* **Generated reference docs.** `scripts/gen_reference.py` reads `Settings` and
  `NUMERIC_RANGES`, the live MCP tool list with its JSON schemas, and the argparse tree,
  and writes `docs/configuration.md`, `docs/mcp-tools.md` and `docs/cli.md`. CI runs
  `--check` on Linux and Windows, so a setting, tool or flag that is added without being
  documented fails the build instead of drifting quietly.
* **Numeric ranges moved into one table.** `config.NUMERIC_RANGES` is now what
  `validate()` enforces and what the docs generator prints, so a limit cannot be enforced
  in one place and described wrongly in another.
* Tests compare each generated file with the code that produces it, and assert that every
  setting and every tool parameter appears in the docs.

### Known limitations

* Cancellation is cooperative; provider requests and ffmpeg runs in flight are not
  interrupted.
* Network adapters remain `verified: false`; `live=true` health checks prove a credential
  works, not that a model's output is good.
* No URL ingestion, no REST server, no distributed workers — see `docs/assessment.md`.
* Segment-level regeneration, target-language alignment and subtitle readability are
  designed in `docs/assessment.md`, not implemented.

## [0.1.2] — 2026-11-18

Second audit: concurrency, configuration and release hardening. No public API, CLI
command or MCP tool name changed.

### Fixed — concurrency

* **The MCP transcription path released its workspace lock before saving (high).**
  `transcribe_video` held the lock only until the provider returned, so a second request
  could claim the same directory and overwrite the first one's transcript — silently, and
  with a valid-looking file at the end. The lock is now held through extraction,
  transcription, the save and the payload build, and released by a context manager on
  any exception, including a failed save. The write path moved to
  `server.run_transcription()` so it can be driven from several threads in tests.
* **Transcript saves were not atomic (medium).** `Transcript.save()` wrote in place; a
  crash could leave a half-written file that the next run would refuse. It now goes
  through the same temp-file + `os.replace` path as every other write.

### Fixed — configuration

* **A malformed boolean was silently `False` (high).** `"flase"`, `"maybe"` and `"2"`
  all parsed as `False`, quietly switching off caching, background preservation or
  ducking. Only `1/true/yes/on` and `0/false/no/off` are accepted now, case-insensitively;
  anything else is a warning and the default is kept.
* **Configuration files could fail obscurely (medium).** `Settings.load()` used to raise
  a raw `JSONDecodeError`, accept a non-object root, ignore unknown keys (a typo like
  `targt_lang` meant the wrong language, silently), accept `NaN`/`Infinity`, and let a
  wrong type surface as a `TypeError` three frames deeper. Every case is now a
  `ConfigError` naming the file, the key and what was expected, and the CLI prints it
  instead of a traceback.

### Fixed — cache locking

* **The cache lock fell back to an unlocked write (high).** After a timeout, `_save_cached`
  wrote anyway, which is exactly how two writers corrupt one file. It now raises
  `CacheWriteError`; the pipeline treats the cache as optional, records a warning and
  carries on with the transcript it already has. The lock gained stale-lock recovery
  (dead pid on POSIX, age on Windows, where probing a pid is unsafe) and a 10 s wait.

### Added

* **Windows CI.** A `windows-latest` / Python 3.12 job runs lint, the full suite, the
  concurrent workspace and MCP tests on their own, the offline demo gate, MCP startup and
  tool registration, and a Windows-specific workspace check (locking, isolation, stale
  lock reclaim, cleanup).
* **`docs/assessment.md`** — the architecture, feature and gap inventory, dependency and
  licence assessment, security risk assessment and the phased roadmap, written before the
  next phase of work rather than after it.

### Fixed — Windows (found by the new Windows job)

* **Concurrent cache writes failed on Windows (high).** The file lock cannot separate two
  *threads* in one process — they share a pid, so a lock held by a sibling thread looked
  live forever and every other writer waited out its timeout. On Windows the release also
  failed with `PermissionError` when another thread had the lock file open. There is now
  an in-process lock per cache directory around the file lock, and deletion/replace go
  through a short retry (`unlink_retry`, and `os.replace` retried inside
  `atomic_write_text`), which is what Windows requires when a file is momentarily open.
  The first Windows run failed on exactly this; it is fixed and re-run, not skipped.

* **`--json` output died on a legacy console (high).** A single `→` in a step detail
  made `json.dumps`/`print` raise `UnicodeEncodeError` on a Windows code page: the
  command exited **1** — the same code as "QC failed" — with empty stdout, so the CI
  gate read a crash as the expected outcome. Machine output is now pure ASCII
  (`ensure_ascii=True`), the console is reconfigured to UTF-8 with
  `errors="replace"` for human output, the gate refuses an exit 1 that came with a
  traceback or empty stdout, and every gate failure prints the stderr tail.

* **A lock being created could be stolen (high).** The lock file is created empty and
  written a microsecond later; a second job that read it in that window saw "no pid" and
  called it stale, reclaimed the directory and joined the first job's workspace. An
  unreadable lock is now treated as live unless it is older than a five-second creation
  grace, in both the workspace lock and the cache lock. Found by a Linux 3.10 CI run.

### Tests

235 (was 143). New: two simultaneous transcriptions of one source, same-named files in
different folders, provider failure, save failure, "the lock is held during the write",
sequential reuse, every boolean field against every documented word, six invalid boolean
values, twelve invalid configuration files, cache lock blocking, stale-lock recovery,
dead-pid recovery, an unwritable cache directory, and a cache failure that must not fail
the dub, a thread holding the cache lock blocking siblings without hanging, and six
threads serialising their writes, a crash that exits 1 not being mistaken for the
expected failure, and the demo run under `PYTHONIOENCODING=cp1252` still emitting valid
ASCII JSON, six threads racing for one job directory, an empty lock file not being
stolen while it is being created, and an old empty lock being reclaimed.

### Known limitations

* No job lifecycle, progress or cancellation yet — see `docs/assessment.md` §4.1.
* Network adapters remain `verified: false`.
* No limits on input size or duration.

## [0.1.1] — 2026-11-17

Production audit release. No public API, CLI command or MCP tool name changed; the
working architecture is untouched. Two behaviours *did* change on purpose, and both are
documented below.

### Fixed — correctness

* **Cache key could collide (high).** `file_fingerprint` used filename + size + integer
  mtime, so two different videos with the same name, size and timestamp shared a
  transcript — and a re-encode that kept its size reused the old one. Keys are now
  SHA-256 of the file's contents, read in 1 MiB chunks, plus the ASR provider, model,
  source language and diarisation flag. The filename is deliberately excluded, so
  renaming or moving a file no longer costs a transcription.
* **Cache writes were not atomic or serialised (high).** A crash mid-write left a
  truncated JSON document that every later run treated as a hard failure, and two
  concurrent jobs could interleave writes. Writes now go through a temp file plus
  `os.replace`, guarded by a lock with a 30 s timeout; a corrupt or outdated entry is
  treated as a miss.
* **Workspaces were named after the file stem (high).** Two lessons called
  `lesson.mp4` in different folders shared a work directory and overwrote each other's
  audio; two concurrent dubs did the same. Jobs now live in
  `<workdir>/jobs/<stem>-<pathhash>/`, take an exclusive lock, get `-2`, `-3` … when
  another live job holds the directory, and reclaim a lock left by a dead process.
* **`DUBFORGE_SPEECH_CPS` arrived as a string (medium).** Fields typed `float | None`
  default to `None`, which told the old coercer nothing, so `12.5` became `"12.5"` and
  broke the timing arithmetic. Such fields are now parsed as JSON. A malformed variable
  is ignored **and reported** (`Settings.env_warnings`, shown by `doctor`) instead of
  being swallowed.
* **A provider returning empty audio looked like success (high).** A 200 with a
  zero-byte body produced a segment with a 0 s duration, a hole in the timeline, and a
  dub that passed timing checks. Renders under 128 bytes or 0.02 s are now a named
  failure for that line, and QC gained `dub_is_audible`.
* **The wrong audio track could be transcribed (medium).** `extract_audio` let ffmpeg
  pick the "best" stream; on a multi-track file that is not necessarily the one the
  picture plays. It now maps `0:a:0` explicitly, and QC warns when the source had more
  than one audio track.
* **Output could be the source file (medium).** The mux failed at the *last* step,
  after every provider had been paid, with "ffmpeg cannot edit existing files in-place".
  It is now refused before the first API call.
* **`duck_and_mix` could clip (medium).** The dub is normalised to a true-peak ceiling,
  but the bed is added on top, so the sum could exceed 0 dBFS. A limiter
  (`alimiter=limit=0.95:level=false`) closes the graph.
* **`--no-duck` produced an invalid filter graph (medium).** An unused `asplit` output
  made ffmpeg refuse the mix; the graph is now built per branch.
* **`edge_tts` failed inside the async MCP server (medium).** `asyncio.run()` raises
  when a loop is already running. Coroutines now go through `run_coroutine()`, which
  uses a short-lived thread on the running loop.
* **DeepL used the free host for paid keys (medium).** Free keys end in `:fx`; using the
  wrong host returns 403, which reads like a bad key. The host is chosen from the key.
* **Sarvam's glossary rewrote the source (low).** Terms were substituted in the *input*
  before translation, corrupting the sentence being translated. The glossary is now
  enforced on the output.
* **Deepgram's query string was hand-built (low).** Now `urlencode`d.
* **Partial translations were invisible (medium).** When a translator answered for only
  some lines, the rest silently kept the source text. QC gained
  `complete_translation`, and the summary shows the count.

### Fixed — CI

* **`|| true` hid every demo failure (high).** The offline demo is *supposed* to fail QC
  (stub translation, tone voice) and exit 1; the old step could not tell that from a
  crash, empty JSON, a missing mp4, an unplayable container or a silent track.
  `scripts/ci_demo_check.py` now asserts the exit code, the JSON, the file, both
  streams, that the only QC failure is `translated`, and the metrics — and fails with a
  named code otherwise. CI also checks that the MCP server registers all ten tools and
  returns structured errors.

### Fixed — MCP server

* **Structured errors everywhere (high).** Every tool now returns
  `{"ok": false, "error": {"code", "message", "hint"}}` with a stable code
  (`invalid_argument`, `missing_file`, `unsupported_media`, `no_provider`,
  `provider_error`, `io_error`, `internal_error`). No stack traces, no raw exception
  strings, no key material.
* **Argument validation (high).** Paths, media type, language codes, numeric ranges,
  provider names, `glossary`/`voice_map` syntax, transcript files and output paths are
  validated before any work starts, with a hint that says what to do.
* **`transcribe_video` wrote to a shared path (medium).** Two concurrent calls shared
  `asr/input.wav`; it now uses a locked job directory.

### Changed behaviour

* Work directory layout is now `<workdir>/jobs/<stem>-<hash>/` with a shared
  `<workdir>/cache/`. `DUBFORGE_WORKDIR` still works; only the paths inside it changed.
  `config.workdir_for()` remains for compatibility but the pipeline no longer uses it.
* The target subtitle file is named for the language (`lesson.hi.srt`, not
  `lesson.dub.srt`).
* QC grew from ten checks to fifteen (`complete_translation`, `voice_selected`,
  `dub_is_audible`, `frame_rate_preserved`, `multiple_audio_tracks`).

### Tests

134 tests (was 56), including: cache collisions with identical names and sizes, cache
reuse after a rename, corrupt and outdated cache entries, eight concurrent cache
writers, workspace isolation for same-named files, lock reclaim, resume after a crash,
content-change cleanup, cleanup refusing a symlink out of the job, `shell=True` and
`eval` scans, structured MCP errors for twelve bad inputs, an empty TTS response, a
multi-audio-track source, a hot mix that must not clip, and the CI demo gate's six
failure classifications.

### Known limitations

* Network adapters remain `verified: false` — the request shapes follow vendor docs and
  are unit-tested against fake responses, but they have not been run against live APIs
  in this release.
* `synclabs` lip sync still needs public URLs and still does not upload for you.
* The rewrite loop cannot shorten lines with `deepl` or `sarvam`; it records a warning
  and falls back to `atempo`.
* Windows lock liveness is judged by age alone (24 h), because probing a pid is not safe
  there.

## [0.1.0] — 2026-11-16

First public release. The pipeline is complete and the offline path is fully tested;
network adapters are written against vendor documentation and marked
`verified: false` until they have been exercised against live APIs.

### Added

* **Pipeline** — probe → separate → transcribe → translate → fit the clock →
  synthesise → assemble → normalise → duck and mix → mux → QC, with a resumable work
  directory and a transcript cache keyed by file fingerprint.
* **Duration-aware translation** — per-language characters-per-second model, a window
  per line derived from the next line's start, and a rewrite loop that sends over-long
  lines back to the translator with an explicit character budget.
* **Voices** — one voice per speaker, assigned once and verified in QC; `--voice-pool`
  and `--voice-map`.
* **Bed preservation** — `demucs` two-stem separation, an instant mid/side fallback,
  sidechain ducking, and a warning when no bed could be produced.
* **QC** — ten checks with `fail`/`warn` severities, a metrics block, `qc.json`, a
  one-screen summary, and a non-zero exit code on failure.
* **Subtitles** — source, target, bilingual SRT and WebVTT on every run.
* **Providers** — 7 recognisers, 6 translators, 6 voices, 3 separators, 2 lip sync,
  each with a spec, a capability list and an honest quality note.
* **CLI** — `doctor`, `providers`, `voices`, `plan`, `dub`, `demo`, `srt`, `mcp`.
* **MCP server** — ten tools, instructions that encode the correct order of operations,
  and transcripts returned as paths rather than payloads.
* **Agent skill** — `skill/SKILL.md` plus provider, quality, prompt and troubleshooting
  references.
* **Docs** — architecture, providers, quality, languages, cost, school deployment,
  platforms, security and consent.
* **Tests** — 56 tests, including an end-to-end dub with real ffmpeg, and one that
  asserts a stub run is reported as a failure.

### Known limitations

* Network adapters are unverified against live APIs in this release.
* No HTTP server: `dubforge.dub()` is a library, and wrapping it is left to the
  deployer on purpose.
* Lip sync needs public URLs and does not upload for you.
* Speech-rate constants are empirical defaults, not measured per language.
