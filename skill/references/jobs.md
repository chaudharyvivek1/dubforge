# Jobs: identity, ownership, attempts, heartbeat

A synchronous `dub_video` is fine for a two-minute clip. Everything longer goes through a
job, so the caller is not blocked and the work survives a dropped connection.

## The tools

| Tool | Use |
|---|---|
| `dubforge_submit_job` | queue a dub; returns a job id immediately (same options as `dub_video`) |
| `dubforge_get_job_status` | status, stage, progress, elapsed, attempts, retry count, error |
| `dubforge_get_job_result` | output paths, QC, `providers_used`, warnings, attempt history |
| `dubforge_list_jobs` | newest first, paginated (`limit`, `offset`), filter by `status` |
| `dubforge_cancel_job` | ask it to stop (cooperative) |
| `dubforge_retry_job` | re-queue a failed / interrupted / cancelled job with its original request |
| `dubforge_cleanup_jobs` | delete finished jobs; **dry run unless `dry_run=false`** |

## Identity: a job is a request, not a file

```
<stem>-<pathhash>-<requesthash>          e.g. lesson-29faaabe-a97ce472
```

`requesthash` covers what changes the *result*: target language, source language, ASR and
translation and TTS providers and models, voice map and pool, glossary, context, style,
emotion, keep-background, gains, ducking, loudness, true peak, audio bitrate, max/min
speed, rewrite rounds, segment limits, tail gap, speech rate, transcript/script reuse,
fallback. Not `workdir`, not `cache`, not logging — those do not change a frame.

Consequences worth stating to a user:

* the same video into Hindi and into Tamil is **two jobs**, both allowed to run at once;
* the identical request twice is **one job** — the response carries `duplicate: true`, so
  say "already running" rather than "started";
* changing the voice, the glossary or a provider produces a **new** job, and the old one's
  output is never presented as the new one's result;
* editing the source file changes its fingerprint, so the next submit is a new job.

Job ids from before 0.2.1 (two parts, no request hash) are still readable; they have no
request snapshot, so they cannot be retried — re-submit instead.

## The lifecycle

```
queued → validating → processing → quality_check → completed
                            ↘ failed   ↘ cancelled   ↘ interrupted
```

Illegal transitions are refused by the store, so a terminal job cannot quietly become
active again. The only way back is `retry`, which:

* increments `attempts` and appends to `attempt_history` (bounded to 50 entries);
* clears the previous attempt's `output`, `audio`, `subtitles`, `qc`, `warnings` and
  `providers_used` — an old file is never presented as the new attempt's result;
* re-runs with the **original** request snapshot, not the current settings.

`completed` with `qc_ok: false` is a finished run and a failed dub. `qc_failed: true` in the
summary exists so you do not have to infer that from two fields.

## Liveness: why a long provider call is not a dead job

* A worker claims a job with a **token** and refreshes a **heartbeat** on a timer
  (`job_heartbeat_interval_s`, default 15 s) while it works — not only between stages.
* Recovery uses the **pid** where a pid means something (POSIX): a live process is never
  declared interrupted, however old the heartbeat looks. A dead pid is interrupted
  immediately.
* Where a pid cannot be probed (Windows), or the worker is on another host, the
  **heartbeat** decides (`job_heartbeat_stale_s`, default 180 s).
* A heartbeat from the future (clock skew between hosts) counts as fresh, not as death.
* Ownership is a file created with `O_EXCL`, so two processes cannot both claim a job, and
  a worker that lost its claim cannot update the job.

## Rules for an agent

1. **Submit once.** The same request returns the running job; check `duplicate`.
2. **Poll, do not spin.** `get_job_status` is cheap; a few seconds between calls is right,
   and tell the user roughly how long the media will take.
3. **Cancel honestly.** "It will stop after the current line" is true; "stopped" is not.
4. **Never claim a job finished because it was submitted.** Only `get_job_result` with
   `qc_ok: true` justifies "your dub is ready".
5. **Collect before cleaning.** `cleanup_jobs` deletes job records *and* their work
   directories.
6. **Read `attempts` before retrying.** The retry budget (`max_retries`) is enforced; a
   second retry of the same failure will not behave differently.
