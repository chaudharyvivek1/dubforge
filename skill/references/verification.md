# Verifying a provider, honestly

Every network adapter in this project ships with `verified: false`. That is not modesty, it is
a fact: no request has been made against those APIs from this repository. This page is the
procedure that changes the fact, the record that proves it changed, and the rules that stop the
record from lying.

## Why a record and not a flag

A boolean in a source file can be flipped by anyone, for any reason, including a bad one. So
the flag is derived: `docs/verification.json` is the evidence, and a test in the suite fails if
an adapter claims `verified: true` without an `ok` row for it
(`tests/test_providers.py::test_network_adapters_are_only_verified_when_the_record_says_ok`).

That means three things hold at once:

* an unverified adapter is a fact, not an error;
* a verified adapter has a dated row naming the version it was tested with;
* a record that was not produced by a real request cannot be added quietly — it has to name a
  provider that exists, a date, and pass the credential tripwire.

## The three commands

```bash
dubforge verify plan                     # what would run, which key, what it costs — no requests
dubforge verify run --yes-live           # the real thing; refuses without the flag
dubforge verify import record.json       # accept evidence produced elsewhere
dubforge verify status                   # what the ledger says right now
```

### `plan`

A table of every network adapter, the environment variable it needs, whether that variable is
present, whether a check exists for its stage, and what the probe would cost in the vendor's
own units:

```
stage     provider        key                         ready  verified cost
asr       deepgram        DEEPGRAM_API_KEY (missing)  no     no       ~2 s of audio
translate sarvam          SARVAM_API_KEY (missing)    no     no       one short sentence
tts       elevenlabs      ELEVENLABS_API_KEY (missing no     no       one short phrase
```

`plan` builds each adapter to ask `missing()` and calls nothing else. There is a test for that:
a spy adapter raises if `plan` ever touches `transcribe`, `translate`, `synthesize` or `voices`.

### `run`

For every adapter with a key, the smallest real call that proves something:

| stage | the call | what is checked |
|---|---|---|
| asr | 2 s of generated tone, 16 kHz mono | the response parses into segments |
| translate | one sentence, en → your target | the text changed and is non-empty |
| tts | one short phrase | audio came back, longer than 50 ms |

A missing key is `skipped`, not `failed`. A vendor error is `failed` with its `error_kind`
(`auth`, `rate_limit`, `timeout`, `schema`). Nothing is printed or stored except the *name* of
the variable — the key never leaves the environment.

### `import`

The realistic case: the machine with keys is your laptop, and the repository is CI. Run
`dubforge verify run --write` there, commit `docs/verification.json`, and CI will see the
evidence. `import` validates before accepting:

* the schema version is one this build reads;
* every `stage/provider` pair exists in the registry;
* no adapter appears twice;
* every `status` is one of `ok` / `failed` / `skipped` / `unsupported`;
* an `ok` row carries an ISO date in `checked_at` — a claim without a date cannot be audited;
* no field looks like a credential (`sk-…`, `api_key=…`, a 32+ character opaque string).

A rejected import lists every problem at once, so one round trip is enough.

## How the flag is actually derived

`verified` is not a field anyone edits. It is computed:

```
spec.verified          a declaration by the adapter author
        │
        ├── local adapter (spec.local) ────────────► verified, source "local"
        │                                           (the test suite proves it every run)
        │
        └── network adapter
                │
                ├── ok row, younger than 180 days ─► verified, source "record", with its age
                ├── ok row, older than 180 days ───► stale, not verified, "re-run to renew"
                ├── failed / skipped row ──────────► not verified, with the vendor's reason
                ├── ok row with no readable date ──► not verified ("cannot be audited")
                └── no row at all ─────────────────► not verified; a *declaration* with no row
                                                     is reported as a contradiction
```

Two consequences worth knowing:

* **A verification expires.** A row from two seasons ago is evidence of a system that has since
  changed its endpoints, models and prices. After `MAX_AGE_DAYS` (180) it stops counting and says
  how old it is, instead of quietly standing.
* **"Local" is a different promise, and is labelled as one.** The stub, the transcript reader,
  ffmpeg centre-removal and the two "none" providers run in this process or in ffmpeg, so the
  test suite exercises them on every commit. That is evidence, but it is not a live API check,
  and the record says `source: "local"` rather than blending the two.

Turning this resolver on found a real modelling gap: `ffmpeg_center` declared `verified` while
being neither local-by-marker nor recorded, so the system had no way to say what it meant. It is
now marked local, which is what it always was.

## What a green row does *and does not* mean

| a row saying `ok` means | it does not mean |
|---|---|
| the credentials worked on that date | that they work today (hence the expiry) |
| the response shape is what the adapter expects | that the voice sounds good |
| the response shape is what the adapter expects | the translation is accurate |
| a payload of the right kind came back | the price you were quoted is current |
| this version of DubForge was tested with it | it will work tomorrow |

Credential health and output quality are different questions. The second one needs a human
listening, which is what `review.json` and `dubforge review` exist for.

## After verification: the calibration dataset

The same run that proves an adapter works can also measure it. `scripts/calibrate.py` renders
a fixed sentence set per language and appends measured samples — characters, duration, voice,
model, method, date — to `data/calibration/samples.jsonl`. A language overrides the estimated
speech rate only at 30 samples or more, and `docs/calibration.md` shows which numbers are
measured and which are still estimates.

```bash
python scripts/calibrate.py --measure --provider edge_tts --languages hi,ta --yes-live
python scripts/calibrate.py --report --write
```

## The honest state today

```
$ dubforge verify status
0 verified, 0 row(s) in the record
counts come from docs/verification.json; an adapter is marked verified only when its row
there says ok
```

Every adapter is unverified, `data/calibration/samples.jsonl` is empty, and every run says
"sentence-level timing only". This page describes how to change that, in an order where each
step produces evidence the repository can check.
