---
name: dubforge
description: Dub a video or audio file into another language with a quality-controlled pipeline. Use when the user asks to dub, translate, re-voice or localise a video or podcast, to add a Hindi/Tamil/Spanish/etc. audio track, to transcribe with speakers and timings, to produce translated or bilingual subtitles, or to check why a dub sounds rushed, out of sync or missing its music. Routes recognition, translation, voice, stem separation and lip sync to swappable providers, then reports QC.
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

### 5. Dub — `dub_video`
Long files are long. Say roughly how long before you start (audio duration × 1–3 for
local engines, faster for APIs), and prefer `dry_run: true` first on an unfamiliar
file.

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
* `references/troubleshooting.md` — error messages and what they actually mean.
