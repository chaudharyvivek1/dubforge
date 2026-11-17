# Speech-rate calibration data

`fit.CPS` holds one estimated characters-per-second figure per language. This directory is
where **measured** replacements live, and it ships empty on purpose: nothing here has been
measured yet, so every language is reported as an `estimate` in the run report.

## `samples.jsonl`

One JSON object per line, one line per measurement. Appended by
`python scripts/calibrate.py --measure --provider <name> --yes-live`, or by hand if you
measured a voice some other way.

| field | meaning |
|---|---|
| `language` | ISO code, e.g. `hi`, `ta`, `en` |
| `chars` | characters in the text, counted **without spaces** (`chars-without-spaces`) |
| `duration_s` | measured spoken duration, in seconds |
| `counting` | must be `chars-without-spaces` — a sample with another convention is rejected |
| `method` | how it was measured: `tts-render`, `human-read`, `studio-recording` … |
| `provider`, `voice` | what produced the audio |
| `script`, `model`, `sample_rate`, `style`, `measured_at` | provenance, optional but expected |

A row missing `language`, `chars`, `duration_s`, `counting`, `method`, `voice` or `provider`
is rejected on load and reported, never silently averaged in.

## `measured_cps.json`

Written by `python scripts/calibrate.py --report --write`. A language appears only once it
has at least **30** samples; its value is the **median** characters per second, and the file
also records p10/p90 and when it was measured. `fit.cps_for()` reads it, and
`fit.source_for()` tells you which source a run used.

## Why a distribution

Speech rate depends on the sentence, the voice and the emotion. A mean hides exactly the
spread that decides whether a line fits its window, so the report carries median, p10, p90
and the count, and an override needs enough samples to be worth trusting.
