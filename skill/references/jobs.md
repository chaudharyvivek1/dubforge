# Jobs: submit, watch, cancel, collect

A synchronous `dub_video` is fine for a two-minute clip. Everything longer should go
through a job, so the caller is not blocked and the work survives a dropped connection.

## The tools

| Tool | Use |
|---|---|
| `dubforge_submit_job` | queue a dub; returns a job id immediately (same options as `dub_video`) |
| `dubforge_get_job_status` | status, stage, progress, elapsed, error |
| `dubforge_get_job_result` | output paths, QC, `providers_used`, warnings |
| `dubforge_list_jobs` | newest first, paginated (`limit`, `offset`), filter by `status` |
| `dubforge_cancel_job` | ask it to stop (cooperative) |
| `dubforge_retry_job` | re-queue a failed / interrupted / cancelled job |
| `dubforge_cleanup_jobs` | delete finished jobs; **dry run unless `dry_run=false`** |

## The lifecycle

```
queued → validating → processing → quality_check → completed
                            ↘ failed   ↘ cancelled   ↘ interrupted
```

* `interrupted` means the process running the job died. It is never a finished dub, and
  it can be retried.
* `failed` carries `error_kind`. Retry `rate_limit`, `timeout` and `network`; do not
  retry `auth` or `bad_request` — fix the key or the request instead.
* `completed` with `qc_ok: false` is a finished *run* and a failed *dub*. Report the
  failures, do not present the file as ready.

## Rules for an agent

1. **Submit once.** The same source path returns the running job rather than starting a
   second one; a duplicate flag in the response tells you which happened.
2. **Poll, do not spin.** `get_job_status` is cheap; wait a few seconds between calls, and
   tell the user roughly how long the media will take.
3. **Cancel honestly.** Say "it will stop after the current line", because that is true.
4. **Collect before cleaning.** `cleanup_jobs` deletes job records *and* their work
   directories; a deleted job's artefacts are gone.
5. **Never claim a job finished because it was submitted.** Only `get_job_result` with
   `qc_ok: true` justifies "your dub is ready".
