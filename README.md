# dubforge

**Dub any video into any language — with several AI providers in one pipeline that
is held to the clock.** One MCP server, one agent skill, swappable recognisers,
translators, voices, separators and lip sync, and a QC report that refuses to call
a bad dub finished.

Most "AI dubbing" is one API call: send the file, get a new audio track back, hope.
It falls over in the same three places every time — the translation is 30% too long
for the actor's mouth, the music disappears, and nobody can tell you whether the
output is usable without watching all of it. dubforge is built around those three.

```bash
pip install "dubforge @ git+https://github.com/chaudharyvivek1/dubforge.git"
dubforge demo
```

`dubforge demo` needs no API key, no GPU and no network. It builds a 9-second clip,
runs every stage for real, and writes an mp4 with a second audio track plus a QC
report — so you can see the machine work before you spend a rupee on it.

---

## What it actually does

```
video ──► probe ──► separate stems ──► transcribe ──► translate ──► fit the clock
             │           │                  │             │              │
             │           │                  │             │        rewrite if too long
             │           │                  │             │              │
             │           └── music/effects  │      per-speaker voice ◄────┘
             │                   │          │              │
             └── picture (copied) │          │        synthesise per line
                                 │          │              │
                                 │          │        atempo to the window
                                 │          │              │
                                 └──────────┴──────► mix (ducked) ──► mux ──► QC
```

| Step | What prevents the failure |
|---|---|
| **Separate** | `demucs` or mid/side keeps the score, the laugh and the door slam; the dub sits *under* them, ducked |
| **Transcribe** | word-level timings from Deepgram / Scribe / Whisper, merged into sentences — never 30-second blocks |
| **Diarise** | one voice per speaker, chosen once and held for the whole film |
| **Translate** | one brief for every vendor: sayable, in character, names intact, glossary enforced |
| **Fit the clock** | every line is budgeted in characters before it is spoken; over-long lines go *back* to the translator |
| **Stretch** | only what is still slightly long, and never past `--max-speed` (1.15x by default) |
| **Mix** | loudness-normalised dub + original bed with sidechain ducking, then a stream copy of the picture |
| **QC** | ten checks; a `fail` exits non-zero and says which line broke |

## Install

```bash
# straight from the repo, one line
pip install "dubforge @ git+https://github.com/chaudharyvivek1/dubforge.git"

# with the MCP server, and the free local engines
pip install "dubforge[mcp,local,sep] @ git+https://github.com/chaudharyvivek1/dubforge.git"

# development
git clone https://github.com/chaudharyvivek1/dubforge.git
cd dubforge && pip install -e ".[mcp,dev]"
```

`ffmpeg` is the only hard dependency, and you may already have it. If you do not,
`pip install imageio-ffmpeg` ships a static binary that dubforge will find on its
own. Check what this machine can actually do — never guess:

```bash
dubforge doctor        # ffmpeg + filters + encoders + packages + keys + which provider each stage would use
dubforge providers     # every adapter, ready or not, verified or not, with an honest quality note
```

`doctor` reports whether a key is set, never its value.

## Dub something

```bash
# 1. what will this cost, and can I run it at all?
dubforge plan lecture.mp4 --to hi

# 2. dub it
dubforge dub lecture.mp4 --to hi \
  --asr deepgram --translator openai --tts elevenlabs \
  --voice-pool <voice-id-1>,<voice-id-2> \
  --glossary "ISRO=इसरो,UPI=यूपीआई" \
  --context "a Class 10 physics lecture on magnetism"

# 3. read the verdict, not just the file
dubforge dub lecture.mp4 --to ta --json | jq '.qc.failures, .qc_summary'
```

Outputs land next to the input, and everything a reviewer needs lands in the work
directory:

```
lecture.hi.mp4            the dub (picture stream-copied, audio re-encoded)
dubforge-work/lecture/
  dub_norm.wav            the dub alone, loudness-normalised
  transcript.dub.json     source + translation + timings + per-line speed
  lecture.source.srt      what was said
  lecture.dub.srt         what the dub says
  lecture.bilingual.srt   both, for proofreading
  lecture.vtt             web subtitles
  qc.json                 the machine-checkable verdict
  report.md               plan, steps, QC and file list in one page
```

`--dry-run` stops after the plan. `--json` on any command gives machine output.

## Use it from an agent

```json
{
  "mcpServers": {
    "dubforge": {
      "command": "dubforge-mcp",
      "env": {
        "DEEPGRAM_API_KEY": "…",
        "OPENAI_API_KEY": "…",
        "ELEVENLABS_API_KEY": "…"
      }
    }
  }
}
```

Ten tools, and the intended order is in the server's instructions:
`dubforge_doctor` → `plan_dub` → `dub_video` → read `qc`. Transcripts come back as
a path and a five-line preview, never as a wall of text.

There is also an agent skill in [`skill/SKILL.md`](skill/SKILL.md) — the workflow,
the failure modes, and the prompts that actually keep a translation inside its time
budget.

## The quality levers, in the order they matter

1. **Translation that fits.** `fit.py` estimates spoken seconds per language from
   characters per second, compares that to the window before the next line starts,
   and sends anything over budget back to the translator with an explicit character
   limit. Two rounds by default. This is the single biggest difference between a dub
   and a translation read out loud.
2. **Voice identity.** Diarisation from the recogniser, one voice mapped per
   speaker, held for the whole file. `--voice-map SPEAKER_00=<id>` when you know
   who is who.
3. **The bed.** Separating the original music and effects and ducking them under the
   dub is what stops the result sounding like a phone call from an empty room.
4. **Emotion and register.** `--style` and `--emotion` reach the vendors that
   support them (ElevenLabs voice settings, OpenAI `instructions`), and `--context`
   reaches the translator, which matters more than either.
5. **Pronunciation.** `--glossary` pins names, acronyms and regional terms so they
   are not silently translated into something else.

The full reasoning, including the arithmetic and the knobs, is in
[`docs/quality.md`](docs/quality.md).

## Providers

Every stage is swappable. `auto` picks by quality first, cost second, and it never
falls back to a stub — a dub that quietly did nothing is worse than an error that
names the missing key.

| Stage | Options | Free / local option |
|---|---|---|
| **Speech recognition** | `faster_whisper`, `deepgram`, `elevenlabs` (Scribe), `groq`, `openai`, `transcript` (reuse one) | `faster_whisper` |
| **Translation** | `openai`, `gemini`, `sarvam` (Mayura), `deepl`, `ollama`, `stub` | `ollama` |
| **Voice** | `elevenlabs`, `sarvam` (Bulbul), `openai`, `google`, `edge_tts`, `stub` | `edge_tts` |
| **Separation** | `demucs`, `ffmpeg_center`, `none` | both |
| **Lip sync** | `none` (default), `synclabs` | — |

`verified: false` in `dubforge providers` means the adapter is written against the
vendor's documentation and has not been exercised against the live API in this
release. That flag exists so nobody ships a paid batch on an unverified path. Only
the offline adapters and the ffmpeg paths are marked verified today.

Details, including each adapter's gotchas: [`docs/providers.md`](docs/providers.md).

## Languages

Indian languages first, because that is the hard case and the reason this exists:
Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia,
Assamese and Urdu, plus English and the usual European set. `fit.py` carries a
speech-rate constant per language, so a Tamil line is not budgeted like an English
one. Coverage per provider, script handling, numbers and RTL: 
[`docs/languages.md`](docs/languages.md).

## Cost

`dubforge plan` estimates it before you spend anything, from list prices with the
source for each row in [`docs/cost.md`](docs/cost.md). A rough shape for a 10-minute
video, using list prices checked in November 2026: about $0.05 for recognition on
Groq, a few cents for translation, and then the voice decides everything — roughly
$1–4 on ElevenLabs, or $0 on `edge_tts`. End-to-end dubbing APIs charge $0.50–$2.20
per *minute* for comparison.

## For a school lab

The whole thing runs offline on a laptop: `faster_whisper` for recognition,
`ollama` for translation, `edge_tts` for voices, `ffmpeg_center` for the bed. No
account, no upload, nothing leaves the machine. Batch a folder, keep a consent log,
and check the QC of every file before it goes near a student:
[`docs/school.md`](docs/school.md).

## What this does not do

* **It is not a translation service.** It orchestrates ones you have keys for.
* **The demo is a tone, not a voice.** QC marks the stub translation as a failure on
  purpose, so a pipeline test can never be mistaken for a finished dub.
* **Lip sync is off by default and needs public URLs.** Reshaping mouths on real
  people is a consent question before it is a technical one.
* **It will not tell you a dub is good.** It will tell you it fits the clock, has one
  voice per speaker, keeps the bed, and does not clip. Whether it *sounds* right is
  still your ears, and the bilingual SRT is how you check the words fast.
* **Cloning a real person's voice needs that person's written permission.** See
  [`docs/security.md`](docs/security.md).

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | stages, data model, files on disk, how to add a provider |
| [providers.md](docs/providers.md) | every adapter: env, capabilities, quality, gotchas |
| [quality.md](docs/quality.md) | the timing model, the QC checks, tuning a dub |
| [languages.md](docs/languages.md) | per-language notes, Indic coverage, numbers, RTL |
| [cost.md](docs/cost.md) | list prices with sources, worked examples, how to cut it |
| [school.md](docs/school.md) | classroom deployment, consent, batch, offline mode |
| [platforms.md](docs/platforms.md) | Linux, macOS, Windows, Docker, sandboxes |
| [security.md](docs/security.md) | keys, consent, student footage, what leaves the machine |

## Contributing

Bug reports with a failing QC report attached are the most useful thing you can
send. A new provider is one file and one registry line — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. Prices quoted in the docs are indicative list prices read in November 2026, not
quotes, and the vendors' terms govern your use of their APIs.
