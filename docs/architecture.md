# Architecture

dubforge is a linear pipeline of swappable stages with one shared data model. The
reason it is not a single API call is that the failures are independent: a perfect
translation with bad timings is unusable, and perfect timings with the music gone is
unusable. Each stage exists to remove one specific way a dub goes wrong, and each
one can be replaced without touching the others.

```
                     ┌──────────────────────────────────────────────┐
 source media ──────►│ ffmpeg.probe → MediaInfo                     │
                     └───────────────┬──────────────────────────────┘
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                             ▼
  source16.wav (ASR)          source48.wav (bed)             picture (untouched)
        │                            │
        │                            ▼
        │                    Separator → bed.wav
        ▼
  ASRProvider.transcribe ──► Transcript{ Segment[] with Word[] }
        │
        ▼
  timeline.windows() ──────► every segment gets `window` seconds
        │
        ▼
  Translator.translate() ──► target_text per segment
        │
        ▼
  fit.needs_rewrite() ─────► Translator.translate(instructions=…) ×N
        │
        ▼
  TTSProvider.synthesize() ─► tts/NNNNN.wav  → atempo to the window
        │
        ▼
  ffmpeg.assemble_dub() ───► dub.wav (timed placement, mixed not concatenated)
        │
        ▼
  ffmpeg.loudnorm() ───────► dub_norm.wav
        │
        ▼
  ffmpeg.duck_and_mix() ───► mix.wav (dub + ducked bed)
        │
        ▼
  ffmpeg.mux() ────────────► out.mp4 (video stream-copied)
        │
        ▼
  qc.report() ─────────────► qc.json + report.md + 4 subtitle files
```

## The data model

Three dataclasses carry everything, and all of them serialise to JSON:

* `Word(start, end, text, speaker?, confidence?)` — what the recogniser returned.
* `Segment(index, start, end, text, speaker, words[], target_text, voice, window,
  raw_duration, rendered_duration, speed, rewrites, notes[])` — one line of dialogue
  and everything learned about it. `Segment.speech` is the translation when there is
  one, the source otherwise, which is what every downstream stage speaks.
* `Transcript(segments[], language, provider, speakers[], duration, notes[])` —
  written to `transcript.dub.json`, reusable as input via `--transcript`.

Nothing in the pipeline passes audio buffers around. Every stage takes a path and
returns a path, which is why a run can be interrupted, resumed from the work
directory, and debugged with `ffplay`.

## Stage by stage

### 1. Probe and decode
`ffmpeg.probe` parses the `ffmpeg -i` banner rather than depending on ffprobe, so a
single static binary is enough. Two audio extractions: 16 kHz mono for the
recogniser (what every ASR API wants, and 10× smaller to upload) and 48 kHz stereo
for the bed.

### 2. Separation
`Separator.separate(audio, out_dir) → (dialogue|None, bed|None)`. `demucs` runs the
real two-stem model. `ffmpeg_center` does a mid/side split, which is instant and
works surprisingly well on broadcast material because dialogue is centre-panned —
it is also honest about failing on mono sources, where it returns no bed and the run
continues with a warning.

### 3. Recognition
`ASRProvider.transcribe(audio, language, diarize) → Transcript`. Word timings are
required wherever the vendor has them: `timeline.merge_words` breaks on a silence
longer than 0.7 s, on sentence punctuation once the line is long enough to be a
sentence, on a speaker change, and always before 12 s / 240 characters. The result
is sentences a person would say in one breath, which is the unit a dub is built on.

### 4. The clock
`timeline.windows(segments, total_duration, tail_gap)` gives every segment the
seconds between its own start and the start of the next line, minus a small tail
gap. A line may spill into the silence after it, never into the next line.

`fit.py` then converts that window into a character budget using a per-language
characters-per-second constant, and classifies the translation:

| ratio (estimated ÷ window) | verdict | action |
|---|---|---|
| ≤ 1.0 | `ok` | nothing; a short line gets a pause |
| 1.0 – `max_speed` (1.15) | `stretch` | atempo, pitch preserved |
| > `max_speed` | `rewrite` | back to the translator with a character limit |

### 5. Translation and the rewrite loop
`Translator.translate(segments, source, target, glossary, instructions, context)`.
`instructions` maps a segment index to a constraint sentence — the character budget
and the reason. The loop runs `--rewrite-rounds` times (2 by default) and stops
early if the vendor cannot shorten (DeepL, Sarvam Mayura), recording a warning
instead of spinning.

### 6. Voices
`timeline.map_voices(speakers, pool, fallback)` assigns one voice per speaker, in
order, reusing only when there are more speakers than voices. `--voice-map` overrides
per speaker. The mapping is written into every segment, so QC can check that no
speaker changed voice halfway through.

### 7. Synthesis and fitting
One line, one request, one file. A failed line is a warning, not a dead run. If the
rendered line is longer than its window by more than 2%, `ffmpeg.stretch` applies
`atempo` (a chain of 0.5–2.0 stages, so any factor works) capped at `--max-speed`.
The applied factor is recorded per segment and checked in QC.

### 8. Assembly
`ffmpeg.assemble_dub(clips, total)` places each clip with `adelay` and mixes with
`amix normalize=0` — placed, not concatenated, so one long line overlaps the gap
after it instead of pushing every later line out of sync. Chunks of 48 clips keep
the filter graph manageable on a feature-length file.

### 9. Mix
`loudnorm` to `-16 LUFS` (streaming standard, and loud enough for a phone speaker),
then `sidechaincompress` on the bed keyed from the dub: the music dips only while
someone is talking, and comes back in the pauses.

### 10. Mux
`-c:v copy`. The picture is never re-encoded, so there is no generation loss and no
long encode: a 40-minute video is muxed in seconds.

### 11. QC
`qc.report` writes twenty-one checks and a metrics block. `ok` is false if any check with
severity `fail` fails, and the CLI then exits 1. See
[quality.md](quality.md#the-checks).

## Jobs (0.2.0, hardened in 0.2.1)

```
<workdir>/
  registry/<job-id>.json          one document per job, atomic writes
  jobs/<stem>-<pathhash>/         the job's workspace (see below)
```

```
queued ──► validating ──► processing ──► quality_check ──► completed
                                │
                                ├──► failed        (an error was raised)
                                ├──► cancelled     (a caller asked it to stop)
                                └──► interrupted   (its process died; derived, never guessed)
```

The design decisions worth knowing:

* **A job is a file, not a row.** No database, no broker, no daemon. `JobStore` reads and
  writes JSON atomically; `JobRunner` is a bounded thread pool. A restart is recovered by
  reconciling `processing` jobs against their pid and heartbeat.
* **The job id is the request.** `<stem>-<pathhash>-<requesthash>`: the path hash keeps the
  workspace identity, the request hash covers everything that changes the result (languages,
  providers, models, voices, glossary, context, mix and timing settings) and nothing that
  does not (`workdir`, `cache`, logging). So one video can have a Hindi job and a Tamil job at
  once, an identical request deduplicates, and a different voice never inherits an old
  output. Ids from 0.2.0 still load.
* **Ownership is a claim file.** Created with `O_EXCL`, holding a token, pid, host and start
  time. `update(token=…)` refuses a worker that lost it; `heartbeat()` and `release()` are
  owner-only. A claim from a dead process is reclaimed; a live one is never stolen.
* **Liveness is a pid *and* a heartbeat.** The worker refreshes the heartbeat on a timer
  (`job_heartbeat_interval_s`), because a provider call can legitimately take minutes. On
  the same host a live pid wins over a stale heartbeat; where a pid cannot be probed
  (Windows) or the worker is elsewhere, the heartbeat decides (`job_heartbeat_stale_s`). A
  heartbeat from the future counts as fresh, so clock skew cannot look like death.
* **Attempts are recorded, not inferred.** `retry` increments `attempts`, appends to
  `attempt_history` (bounded to 50), clears the previous attempt's result fields and re-runs
  the request snapshot stored with the job.
* **`interrupted` is derived, not stored hopefully.** A job with a dead pid (or a stale
  heartbeat, which is all Windows allows) becomes `interrupted` on read. It is never
  reported as completed.
* **Cancellation is cooperative.** `pipeline.dub(cancel=…)` is consulted between stages
  and before every paid call. An ffmpeg run in progress is not killed; the next boundary
  is the next line of dialogue at worst. This is stated in the tool output rather than
  promised away.
* **Duplicate execution is refused in-process** by the runner's in-flight set and across
  processes by the claim file — verified with a real subprocess in the tests.
* **Illegal transitions are refused.** `TRANSITIONS` is enforced by the store; a terminal
  job cannot become active again except through `retry`.
* **Retries are operation-aware.** A 429/5xx answer is retried; a network failure is retried
  only when the call is idempotent or carries a vendor-documented idempotency key. Otherwise
  the error is `kind="ambiguous"` and says the request may have been billed.

## Files on disk

```
dubforge-work/
  jobs/<stem>-<pathhash>/         one directory per source *path*, locked while in use
    .lock                         pid + host + start time; stale locks are reclaimed
    manifest.json                 source, content fingerprint, per-step state
    source16.wav  source48.wav    decoded audio
    stems/                        separated bed
    transcript.dub.json           source + translation + timings + speeds
    tts/00000.wav …               one file per line
    dub.wav  dub_norm.wav  mix.wav
    *.srt  *.vtt                  reviewable subtitles
    qc.json  report.md            the verdict, and the story of the run
  cache/<content-key>/transcript.json
                                  shared across jobs, written atomically under a lock
```

### Why the workspace looks like that

`jobs/<stem>-<pathhash>` rather than `jobs/<stem>`: a filename is not an identity. Two
lessons called `lesson.mp4` in different folders used to share a work directory and
overwrite each other's audio; the path hash makes that impossible.

The lock is what makes concurrent dubs safe. A second job on the same file gets
`…-2` rather than a shared directory, and a lock left behind by a killed process is
reclaimed when its pid is gone (or after 24 hours on Windows, where probing a pid is
not safe).

`manifest.json` is what makes an interrupted run resumable: it records the content
fingerprint and each finished step. Re-running the same file continues in the same
directory; re-running after the file changed clears artefacts that belong to bytes
that no longer exist.

The transcript cache is keyed by **SHA-256 of the file's contents** plus the ASR
provider, model, source language and diarisation flag — never by name or mtime. Two
identical copies of a file share one transcription; two different files with the same
name, size and timestamp share nothing. Writes go through a temp file and `os.replace`,
so a crash mid-write cannot leave half a JSON document behind, and a corrupt entry is
treated as a miss rather than an error.

## Adding a provider

1. Subclass the right base (`ASRProvider`, `Translator`, `TTSProvider`,
   `Separator`, `LipSync`) in the matching module under `providers/`.
2. Declare a `Spec` — `name`, `kind`, `title`, `env`, `packages`, `capabilities`,
   `languages`, `verified`, `quality`, `notes`, `docs`. `quality` is a sentence a
   stranger can use to choose; leave it empty and the test suite fails.
3. Add one line to `REGISTRY` and, if it should be a default, to `AUTO`.
4. Add it to `docs/providers.md`.

`tests/test_providers.py` enforces the contract: every registered adapter must have
a matching spec, a title and a quality note, and no spec may leak a secret.

## Design rules worth keeping

* **Paths, not payloads.** No stage returns audio or text in bulk. This is why the
  MCP tools are safe to call from an agent with a small context window.
* **Identity is content, not metadata.** Cache keys hash bytes. Anything keyed by a
  filename is a collision waiting for the second file with that name.
* **Destructive operations re-check their boundary.** Cleanup resolves every path and
  refuses anything outside the job directory, including through a symlink. Job cleanup
  validates the job id before touching the filesystem and only deletes directories under
  `<workdir>/jobs/`.
* **A refusal beats a warning.** Limits (size, duration, runtime, cost) stop a run with
  the setting to change; a limit is never silently disabled.
* **Never silently switch providers.** Fallback is opt-in, announced in the warnings, and
  the provider that actually ran is recorded separately from the one requested.
* **Fail loudly, continue cheaply.** A missing bed or a failed line is a warning; a
  missing key or an untranslated dub is an error. Never a silent success.
* **Measure, don't assume.** Every duration that matters is measured with ffmpeg
  after the fact; the estimate is only used to *plan*.
* **The picture is a passenger.** Nothing in the pipeline touches video frames.
