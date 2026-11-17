# Batch: a folder, not a loop of tool calls

```bash
dubforge batch lessons/ --to hi --estimate
dubforge batch lessons/ --to hi --force          # re-dub even what already passed QC
dubforge batch lessons/ --to hi --stop-on-error  # default is to keep going
```

`dubforge batch` is the same pipeline, one job per file. There is no second implementation,
so a fix in the pipeline is a fix in batch mode, and a batch is exactly as trustworthy as a
single run.

## What it guarantees

| Guarantee | How |
|---|---|
| one broken file does not stop the rest | each file is its own job; failures are recorded and the batch continues |
| no file is dubbed twice in one run | sources are deduplicated by *resolved* path, so a symlink and its target count once |
| the input folder is never polluted | outputs go to `<workdir>/out/`, and files inside the workdir are never inputs |
| a previous good result is not replaced | a completed, QC-passing output is skipped unless `--force` |
| two folders with the same filename do not collide | the relative structure under the batch root is preserved: `class-a/lesson.hi.mp4`, `class-b/lesson.hi.mp4` |
| a name still collides (flattened folders, an older different file) | a deterministic 6-character hash of the resolved source path is appended, so re-running lands on the same name instead of accumulating copies |
| the source is never overwritten | `output_for()` refuses to return the source path |
| its own outputs are never re-dubbed | every output any job has produced is excluded from input discovery |
| thousands of jobs do not slow input discovery | outputs are read as raw JSON without building job objects or reconciling state |

## Resume

A batch is resumable by construction: re-running it skips files whose job completed with
`qc_ok: true` and whose output still exists. A file that failed, failed QC, or was
interrupted is retried. `--force` overrides the skip; it still refuses to write over a
source, and still disambiguates rather than overwriting an existing file it did not
produce.

## A school or studio workflow

```bash
# 1. what will this cost, before spending anything
dubforge batch lessons/ --to hi --estimate

# 2. run it, one job at a time on a lab machine
export DUBFORGE_MAX_CONCURRENT_JOBS=1
export DUBFORGE_MAX_ESTIMATED_USD=6
dubforge batch lessons/ --to hi --json > batch.json

# 3. what needs a human
dubforge jobs list --status failed
dubforge jobs list --status completed      # check the QC column, not just the status

# 4. retry only what is worth retrying
dubforge jobs retry <job-id>

# 5. clean up a term's worth of intermediates
dubforge jobs cleanup --older-than 30 --yes
```

`--json` gives `counts` (`total`, `completed`, `qc_failed`, `failed`, `cancelled`,
`interrupted`, `skipped`) and one row per file with its job id, status, QC verdict, output
path and error. Use the counts for a summary and the rows for the ledger.

## Reading the result honestly

* `counts.completed` counts runs that finished **and** passed QC. A batch where every file
  ran but none passed is `0 completed, N failed QC` — say that, not "done".
* Exit code is `0` only if nothing failed and nothing failed QC; `1` if the batch ran but
  some files did not pass. A non-zero exit with a full ledger is a result, not a crash.
* `qc_failed` files have a real output on disk. It exists; it is not approved.
