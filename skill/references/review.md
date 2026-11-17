# Handing a dub to a human reviewer

Automatic QC clears the mechanical failures: a line with no audio, a timing collision, a
missing track, a clipping mix. It cannot tell you whether the translation is faithful, the
performance believable, or the voice close to the original. That is a human's job, and this
is the artifact that makes it a twenty-minute job instead of an afternoon.

## `review.json`

Every run writes `review.json` beside `qc.json`. One row per line:

| field | why a reviewer cares |
|---|---|
| `source_text` | what was actually said |
| `target_text` | what the dub says |
| `speaker` | who is talking, so two voices can be compared per person |
| `source.start` / `source.end` | where it was said |
| `target.window` / `target.rendered_duration` / `target.speed` | whether it was squeezed, and by how much |
| `voice` | which voice id read it |
| `audio` | the exact file for that line, to listen to in isolation |
| `fit.verdict` / `fit.ratio` | `ok` / `stretch` / `rewrite`, and how far over it was |
| `alignment` | `measured` or `sentence_level_only` — do not read precision into a sentence-level timing |
| `regenerated` | whether this line was re-rendered after an edit |
| `warnings` | per-line notes collected during the run |

Top level: `qc` (failures and warnings), `alignment` (coverage), `reused_segments` /
`rendered_segments`, `providers_used`, and a `note` that restates the alignment rule.

## The review loop

```bash
dubforge jobs list --status completed           # pick a job (check the QC column)
jq -r '.segments[] | "\(.index)\t\(.target_text)\t\(.fit.verdict)"' \
   dubforge-work/jobs/<job>/review.json | head -40
dubforge regen <job-id> --set 7="छोटा वाक्य।" --set 12="दूसरा संपादन।"
```

The second command re-renders two lines and writes a new file. Everything else is reused.

## What to tell a user, precisely

* "QC passed" means the mechanical checks passed. Say that, not "the dub is correct".
* "Sentence-level timing" means the line is placed as a unit; word timings were not measured
  and were not invented.
* A `warn` is worth a listen; a `fail` blocks the release. `qc.json` carries both `declared`
  (the class) and `severity` (this run's outcome) so the two are never confused.
