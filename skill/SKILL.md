---
name: dubforge
description: Dub a video or audio file into another language with a quality-controlled pipeline. Use when the user asks to dub, translate, re-voice or localise a video or podcast, to add a Hindi/Tamil/Spanish/etc. audio track, to batch-dub a folder of lessons, to transcribe with speakers and timings, to produce translated or bilingual subtitles, to check on or cancel a long-running dub job, or to check why a dub sounds rushed, out of sync, missing its music, or cost more than expected. Routes recognition, translation, voice, stem separation and lip sync to swappable providers, runs long work as inspectable jobs, and reports QC.
---

# dubforge

You are driving a dubbing pipeline over MCP. The tools do the audio; **you** own the
brief, the provider choice, the review loop, and telling the truth about what came
out.

## The one rule that matters

**A run finishing is not a dub succeeding.** `dub_video` returns `qc.ok`. If it is
false, read `qc.failures` and fix the cause before you tell the user anything is done.
Never report "your video has been dubbed" on the strength of an exit code.

Second rule: **never paste a transcript into the conversation.** It is a file. Pass
the path, quote at most five lines.

Third rule: **a retry can cost money.** A non-idempotent request that timed out may already
have been billed. Never re-run a synthesis, a translation or a transcription after an
`ambiguous` failure without saying so out loud.

## Workflow

### 1. Diagnose — `dubforge_doctor`
Never guess why a stage is unavailable, and never tell the user "that isn't supported"
before checking. It returns ffmpeg (with the missing filters named), which optional
packages exist, which keys are set, and what `auto` would pick for each stage.

If a stage is not ready, say which variable or package is missing and what it costs —
do not silently switch the user to a stub.

### 2. Brief — `plan_dub`
Never call `dub_video` from a bare request. `plan_dub` returns the media facts, the
chosen providers, an estimated cost in USD and the blockers. Read it back to the user
when the cost is non-trivial (anything above a few dollars) or when the target language
is Indic and the provider choice changes the result.

Ask before spending money when the answer is not obvious:

* **Which languages**, source and target. `--from auto` is fine; the target never is.
* **Who is on screen**, if it matters for voice choice or lip sync.
* **Whose voice.** Cloning a real person requires that person's written permission —
  say so once, plainly, and offer a stock voice as the alternative.
* **Delivery**: audio-only dub (default, cheap, honest) or lip sync (needs public URLs,
  reshapes mouths, different consent question).

If the user has no keys, say what the free path is (`faster_whisper` + `ollama` +
`edge_tts`, all offline or keyless) rather than refusing.

### 3. Choose providers
Let `auto` route unless you have a reason. Reasons to override:

* **Speakers matter** (interview, drama, panel) → `asr: deepgram` or `elevenlabs`.
  Diarisation is what stops two actors sharing one voice; without it every line is
  `SPEAKER_00`.
* **Indic target** (hi, ta, te, bn, mr, gu, kn, ml, pa, or, as) → `tts: sarvam` for the
  voice, and consider `translator: sarvam` for idiom. But note: Sarvam's translator
  **cannot shorten a line on request**, so keep an LLM translator if the timing
  matters — which it always does.
* **Budget is the constraint** → `asr: groq` (≈$0.0007/min), `tts: edge_tts` (free),
  `translator: openai` for the rewrite pass.
* **Nothing may leave the machine** → `asr: faster_whisper`, `translator: ollama`,
  `separator: demucs`.
* **A human already wrote the transcript** → `transcript: <path>`, free and instant.
* **Unverified adapters**: `list_providers` marks them. Say so before a paid batch.

### 4. Prepare the words — this is where quality comes from
Three inputs change the output more than the voice choice:

* `context` — what the video is about. *"Class 9 biology lesson on photosynthesis"*
  fixes terminology that no model can guess.
* `glossary` — `"ISRO=इसरो,UPI=यूपीआई,2026=दो हज़ार छब्बीस"`. Names, acronyms, numbers,
  place names. Use it whenever the source has any.
* `style` — register and delivery. *"warm, unhurried teacher, आप-form"*. This reaches
  the translator as well as the voice, which is the point.

### 5. Dub — `dub_video`, or `dubforge_submit_job` for anything long
`dub_video` blocks until the dub is finished. For a long file use `dubforge_submit_job`,
which returns a job id immediately, then poll `dubforge_get_job_status` (status, stage,
progress, elapsed) and collect with `dubforge_get_job_result` (paths, QC, `providers_used`).

```
dubforge_submit_job → {"job": {"job_id": "lecture-a1b2c3d4", "status": "queued"}}
dubforge_get_job_status → {"job": {"status": "processing", "stage": "synthesise", "progress": 0.45}}
dubforge_get_job_result → {"output": "...", "qc": {...}, "providers_used": {...}}
```

* Cancellation is **cooperative**: `dubforge_cancel_job` is checked between stages and
  before each paid call. Say that plainly; do not imply an in-flight request is killed.
* A job left `interrupted` (its process died) can be re-queued with
  `dubforge_retry_job`, within the configured retry budget.
* `dubforge_list_jobs` is paginated and newest-first; pass `status` to filter.

### 5a. One source, many languages

A job id is the *request*, not the file: `<stem>-<pathhash>-<requesthash>`, where the
request hash covers target language, providers, models, voices, glossary, context, timing
and mix settings. So:

* `dubforge_submit_job` for the same video into Hindi and into Tamil returns **two** jobs
  and they run independently;
* submitting the identical request twice returns the job already running (check the
  `duplicate` flag rather than assuming a new run started);
* a retry (`dubforge_retry_job`) re-runs the **original** request, not whatever the
  current settings are — so never "fix" a job by retrying it after changing defaults.

### 5b. Batch a folder, not a loop of calls

For more than a couple of files use `dubforge batch <folder> --to hi` (CLI) rather than
calling the tool in a loop: it isolates failures, writes outputs outside the input folder,
never overwrites a previous QC-passing result, and keeps the relative folder structure so
`class-a/lesson.mp4` and `class-b/lesson.mp4` cannot collide. Read `references/batch.md`.

### 5b2. Editing one line, not the film

The MCP tool `dubforge_regenerate_segments` (CLI: `dubforge regen`) re-dubs a job with edited
lines and reuses every untouched render:

```
dubforge_regenerate_segments job_id="lecture-a1b2c3d4" edits="3=यह संपादित पंक्ति है।"
→ {"output": "…/lecture.hi.r1.mp4", "rendered_segments": 1, "reused_segments": 18}
```

* Only edited lines are synthesised again; the rest come from the segment store, whose
  records carry the audio's SHA-256 (a truncated file is a miss, not a silent hole).
* The result is written to a **new** file. Never tell a user the old file was replaced.
* A retry or a fresh submit re-renders everything; regeneration is the cheap path and it
  needs a job that already finished with a transcript.

### 5b3. The review loop

```
dubforge_export_review job_id="lecture-a1b2c3d4" out="review.json"
→ {"segments": 300, "schema": 1}
```

The user edits `edited_target_text` on the lines that need it, then:

```
dubforge_validate_review review_path="review.json"   → {"ok": true, "edits": 5}
dubforge_apply_review review_path="review.json"      → {"applied": 5, "rendered_segments": 5,
                                                        "reused_segments": 295, "stems_reused": 1}
```

* A review file carries the job id and the source's content fingerprint: a file made from a
  different version of the video is **refused**, not applied.
* Only edited lines are synthesised; the bed, every unchanged render and — since 0.5.0 — the
  dialogue track are reused. The track is *spliced*: the changed lines' own ranges are muted
  and the new renders mixed in at the same starts. Normalisation, ducking, mix and mux still
  run, so never claim "only five lines were processed".
* When a changed line overlaps another line, the splice is refused and the track is assembled
  from scratch; `review.json` then says `work.dialogue_mode: "assembled"`. Report what it says.
* Applying the same file twice is a no-op.

### 5c. Cost safety when a provider is slow or failing

A timeout on a non-idempotent call (synthesis, translation, transcription) is **not**
retried by default: the vendor may have accepted and billed it. The error is
`provider_error` with kind `ambiguous`, and re-running that line may cost twice. Only
`rate_limit`, `server` and `timeout` *answers* are retried automatically, and only where
the vendor documents an idempotency key. Tell the user this instead of retrying blindly.

### 5d. Know the limits before you queue
`dubforge_get_capabilities` answers what this install can do; `dubforge_estimate_cost`
answers what a run would cost at list prices and whether the configured limits allow it.
Limits are refusals, not warnings: input size, input duration, wall-clock run time,
estimated cost, concurrent jobs. A `limit_exceeded` error names the setting to change —
report it to the user instead of retrying.

### 6. Read the QC, then report
`qc_summary` is the one-screen verdict. Report it as it is:

```
QC: PASS   target=hi
  lines 214/214 rendered, 12 rewrite(s)
  timing worst overrun 0.18s, max speed 1.15x
  loudness -16.0 LUFS, peak -1.8 dBFS
  duration 612.4s → 612.4s
```

Then point at the files: the mp4, `*.bilingual.srt` for proofreading, `qc.json` for the
machine, `report.md` for the story of the run.

If `qc.ok` is false:

| failure | what it means | what to do |
|---|---|---|
| `translated` | the stub translator ran | configure a real translator; this was a test |
| `timing_fits` | a line overran its window by >0.35s | raise `rewrite_rounds`, use an LLM translator, or split that line |
| `speech_rate` | a line was squeezed past `max_speed` | same, plus lower `max_speed` to 1.08 for a calmer read |
| `one_voice_per_speaker` | the speaker mapping changed | pass an explicit `voice_map` |
| `every_line_has_audio` | synthesis failed for some lines | read `warnings`; usually a key, a voice id or a rate limit |
| `no_empty_lines` | the translator returned nothing for a line | re-run; if it persists, that line's source may be music-only |

Job-level states worth knowing:

| state | meaning | what to do |
|---|---|---|
| `interrupted` | the process running the job died (dead pid, or no heartbeat) | never report as a dub; `dubforge_retry_job` re-queues the same request |
| `failed` | an error was raised; `error_kind` says which | retry `rate_limit`/`timeout`/`server`; fix `auth`/`bad_request`/`ambiguous` |
| `completed` + `qc_ok: false` | the run finished, the dub failed QC | report `failures`; do not present the file as ready |
| `cancelled` | a caller asked it to stop | the artefacts of that attempt are not a result |

`error_kind: ambiguous` means a non-idempotent request timed out and may have been billed.
Do not retry it automatically; check the provider dashboard or re-run that one line
deliberately.

Warnings (loudness, headroom, duration, background) do not block delivery but must be
mentioned: "the music bed could not be separated on this mono source, so the original
dialogue is faintly under the dub."

### 7. Offer the review loop
The honest close is: *the words are in `lesson.bilingual.srt`, watch three minutes.*
Offer a re-run with changes rather than claiming perfection.

## Things that go wrong and are not the pipeline's fault

* **Latin-script Hindi in, Latin-script Hindi out.** Ask for the target script in
  `context`; a Devanagari or Tamil string is what makes a voice sound native.
* **Numbers read in English.** Pin them in `glossary`.
* **A dubbed joke that is no longer funny.** Translation, not TTS. `context` and
  `style` are the levers; a second pass on the SRT is the fix.
* **Mono source.** `ffmpeg_center` cannot separate it. Say so; suggest `demucs`.
* **A 90-minute film.** Expect the bill and the time to scale; run `plan_dub` first and
  get agreement.

## References

* `references/providers.md` — every adapter, when to pick it, what it costs, its gotchas.
* `references/quality.md` — the timing model, the QC checks, the tuning table.
* `references/prompts.md` — briefs and glossary patterns that measurably improve output.
* `references/jobs.md` — the job tools: identity, claims, attempts, heartbeat, cancellation.
* `references/batch.md` — folders, collisions, resume, a school or studio workflow.
* `references/reliability.md` — retries, idempotency, provider health, fallback, events.
* `references/review.md` — the review loop (`export` → edit → `validate` → `apply`), the
  `review.json` artifact, alignment honesty, and what a re-run actually reuses.
* `references/troubleshooting.md` — error messages and what they actually mean.
