# Cost

`dubforge plan video.mp4 --to hi` estimates the bill before anything is spent, from
the table in `src/dubforge/pricing.py`. Every number below is a **list price read
from the vendor's own pricing page in November 2026**, before taxes and volume deals.
Prices change; treat them as estimates to re-check, not as a quote.

## Speech recognition — per audio minute

| Provider | Model | USD / min | Source |
|---|---|---|---|
| `groq` | whisper-large-v3-turbo | 0.00067 | groq.com/pricing |
| `openai` | gpt-4o-mini-transcribe | 0.0030 | openai.com/api/pricing |
| `elevenlabs` | Scribe v2 | 0.0037 | elevenlabs.io/pricing |
| `deepgram` | Nova-3 pre-recorded | 0.0043 | deepgram.com/pricing |
| `openai` | whisper-1 | 0.0060 | openai.com/api/pricing |
| `faster_whisper` | local | 0 | your hardware |

Recognition is the cheapest part of a dub and the one where the cheap option is often
the better one: Groq's turbo model is ~9× cheaper than Whisper-1 and usually as
accurate. Pay for Deepgram when you need **diarisation**, which is worth the money
because it is what keeps two actors from sharing one voice.

## Voice — per 1,000 characters

| Provider | Model | USD / 1k chars |
|---|---|---|
| `edge_tts` | free endpoint | 0 |
| `openai` | tts-1 | 0.015 |
| `google` | Neural2 | 0.016 |
| `deepgram` | Aura-2 | 0.030 |
| `elevenlabs` | Flash v2.5 | 0.040 |
| `elevenlabs` | Multilingual v2 | 0.080 |
| `sarvam` | Bulbul v2 | see dashboard (not published as a list rate at the time of writing) |

**The voice is the bill.** A 10-minute video is roughly 9,000 characters of speech, so
ElevenLabs Multilingual v2 is ~$0.72 per 10 minutes of finished audio — but a
conversation-heavy 10 minutes is more like 14,000 characters, and a re-render doubles
it. Budget from characters, which `plan` reports, not from minutes.

## Translation

LLMs bill per token, and a dubbing script is small: 10 minutes of dialogue is about
1,400 words in, 1,700 out. On current mid-tier pricing that is cents. `plan` reports
an estimate and marks it as a rough guess — the token counts are the vendor's, not
ours.

## For comparison: end-to-end dubbing APIs

| Service | USD / source minute | Notes |
|---|---|---|
| ElevenLabs Dubbing v1 | 0.33 (watermark) / 0.50 (none) | 29 languages, automatic speaker detection |
| ElevenLabs Dubbing v2 | 2.20 | 90+ languages, voice and emotion preserved |
| Azure Video Translation | ~0.33 | $5/hour input + $15/hour output, standard voice |

Ten minutes: **$5.00** on Dubbing v1, **$22** on v2, **~$3.30** on Azure.

## Worked example — 10-minute lecture, English → Hindi

| Stage | Choice | Cost |
|---|---|---|
| Recognition | Groq whisper-turbo, 10 min | $0.007 |
| Diarisation | (not needed, single speaker) | — |
| Translation | one LLM pass + one rewrite pass, ~14k chars | ~$0.02 |
| Voice | ElevenLabs Multilingual v2, 9k chars | $0.72 |
| Separation | `ffmpeg_center` | $0 |
| **Total** | | **≈ $0.75** |

The same run with `edge_tts` instead of ElevenLabs is **≈ $0.03**, and with
`faster_whisper` + `ollama` + `edge_tts` it is **$0.00** — slower and less natural,
but free and entirely offline.

## How to cut the bill without wrecking the dub

1. **Recognise once.** The transcript is cached by file fingerprint + provider +
   model. Re-running with a different translator or voice costs nothing for ASR.
2. **Translate twice, synthesise once.** Spend the rewrite rounds on text; every
   avoided re-render is the expensive stage.
3. **Fix lines before you voice them.** Edit `transcript.dub.json`, then re-run with
   `--transcript`. Hand-fixing ten bad lines costs minutes; re-running a full dub to
   fix ten bad lines costs the whole bill again.
4. **Preview cheaply.** Run the first 2 minutes with `edge_tts` to check the
   translation and timing, then render the full thing on ElevenLabs.
5. **Use `--dry-run` in CI.** It resolves providers and prints the estimate without
   touching an API.
6. **Prefer `stretch` over `rewrite`** for short overruns: atempo is free, a rewrite
   pass is tokens. The default 1.15× cap is a reasonable place to draw that line.

## What is never charged by dubforge

The tool itself is MIT and free. There is no proxy, no account, no telemetry: your
keys go straight to the vendor from your machine, and nothing is uploaded anywhere
except the audio or text a provider needs to do its job. If a stage runs locally
(`faster_whisper`, `demucs`, `ollama`, `edge_tts`), no network call is made for it at
all.
