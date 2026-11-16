# Quality: what the numbers mean

## The three knobs that change the result most

1. `rewrite_rounds` (default 2). Each round sends over-long lines back to the
   translator with an explicit character budget. Two rounds is right for a competent
   LLM; three is worth trying on dense dialogue (fast banter, comedy).
2. `max_speed` (default 1.15). The fastest a line may be played back. 1.15 is
   inaudible; 1.25 is noticeable; 1.4 sounds like a different person. Lower it to 1.08
   for material that must sound calm, and accept more rewrites.
3. `context` + `glossary`. Terminology, register and names. This is free and it is
   usually the difference between "translated" and "dubbed".

## Reading the QC

| field | meaning | healthy |
|---|---|---|
| `rendered / segments` | lines that got audio | equal |
| `rewrites` | lines sent back for being too long | 5–20% of lines is normal for a dense language pair |
| `worst_overflow_s` | the worst line that still ran long | < 0.35 |
| `max_speed_applied` | the most a line was compressed | ≤ `max_speed` |
| `speech_seconds_original` vs `_dubbed` | how much talking there is | dubbed within ~15% is normal |
| `integrated_lufs` | loudness | within ±2.5 of target (−16) |
| `peak_db` | headroom | ≤ −0.5, ideally −1.5 |
| `input_seconds` / `output_seconds` | runtime drift | identical; the picture is stream-copied |

## Why a line has a window, not a duration

The window is the time before the *next* line starts, minus a tail gap. A line may
spill into the silence after it; it may never push the next line later, because the
picture does not move. This is why an over-long line is a rewrite problem and not a
"just speed it up" problem.

## Estimating before you render

`plan_dub` reports `est_translated_chars`. Divide by the language's characters per
second (en 15, hi 13.5, ta 11.5, ja 8.5) to sanity-check that a target language will
fit the runtime. A 10-minute English video is about 9,000 characters; if the plan
predicts 14,000 for the target, expect heavy rewriting and a denser read.

## What a human must still check

* Is it the same person? (Timbre, age, energy.)
* Is the register right? (Teacher vs friend vs official.)
* Is the joke still funny, the threat still a threat?
* Do the names sound the way the audience says them?

`*.bilingual.srt` is the fast way to check the words: source above, target below.
