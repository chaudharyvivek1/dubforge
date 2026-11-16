# Providers

Every stage is an adapter with a declared spec. `dubforge providers` prints this
table live, including whether each one is ready on *this* machine and exactly what is
missing.

Two flags mean something specific:

* **ready** — the environment variable, package or binary is present.
* **verified** — the adapter has been exercised against the live API in this
  release. Everything network-facing is currently **unverified**: the request shapes
  follow each vendor's published docs, but treat the first output as a test, not a
  delivery. The offline adapters and the ffmpeg paths are verified.

## Speech recognition (`--asr`)

| name | env | languages | diarisation | notes |
|---|---|---|---|---|
| `faster_whisper` | — (pip) | 99 | no | Local, free, CPU. Model downloads on first use. One speaker per run. |
| `deepgram` | `DEEPGRAM_API_KEY` | 30+ | **yes** | Nova-3. Best word timings and speaker labels; the default for drama and interviews. |
| `elevenlabs` | `ELEVENLABS_API_KEY` | 90+ | **yes** | Scribe v2. Strong on accents and names; flags laughter and music, which helps QC. |
| `groq` | `GROQ_API_KEY` | 99 | no | Whisper large-v3-turbo at very high speed. Cheapest per minute by a wide margin. |
| `openai` | `OPENAI_API_KEY` | 99 | no | Whisper-1 or `gpt-4o-transcribe`. Simple, no speaker labels. |
| `transcript` | — | — | as supplied | Reuse a `transcript.dub.json` or an `.srt` with `--transcript`. Free, instant, and the right choice when a human already wrote the transcript. |
| `stub` | — | — | script labels | Offline test double. Needs `--script`; timings come from silence detection. |

Choosing: **Deepgram** when speakers matter, **Groq** when budget matters,
**faster_whisper** when the machine must not talk to the internet, **transcript** when
you already have the words.

## Translation (`--translator`)

All LLM adapters share one prompt (`providers/translate.py:SYSTEM` + `RULES`): say
what the line means, fit the time, stay in character, speak rather than write, keep
names and numbers, preserve placeholders, output JSON only.

| name | env | notes |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | Best general instruction-following on the length constraint. `--translate-model` to pin. |
| `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Good on Indic languages, long context, cheap. |
| `sarvam` | `SARVAM_API_KEY` | Mayura. Purpose-built for 11 Indian languages. **Cannot shorten on request** — the rewrite loop records a warning and atempo handles timing. |
| `deepl` | `DEEPL_API_KEY` | Excellent European pairs, cheapest per character. No duration control, no Indic beyond a few. |
| `ollama` | — (local server) | Anything you have pulled. Free, private, offline. Readiness actually probes the server. |
| `stub` | — | Identity. QC fails the run on purpose. |

**The rewrite loop only works with an LLM.** That is not a small detail: if your
translator cannot shorten a line, every over-long line ends up compressed by atempo
instead, and you hear it. Use `sarvam` or `deepl` for the first pass and
`--translator openai` when you need the timing pass.

## Voice (`--tts`)

| name | env | cloning | Indic | notes |
|---|---|---|---|---|
| `elevenlabs` | `ELEVENLABS_API_KEY` | **yes** | yes | The reference for identity and emotion. `eleven_multilingual_v2` ~$0.08/1k chars. |
| `sarvam` | `SARVAM_API_KEY` | no | **best** | Bulbul v2. Handles Devanagari, Tamil, Telugu, Bengali script correctly. Speakers: `anushka`, `abhilash`, `manisha`, `vidya`, `arya`, `karun`, `hitesh`. |
| `openai` | `OPENAI_API_KEY` | no | good | `gpt-4o-mini-tts` accepts an `instructions` field, which is where `--style` goes. |
| `google` | `GOOGLE_API_KEY` | no | good | Pass a full voice name in `--voice-pool`, e.g. `hi-IN-Neural2-A`. |
| `edge_tts` | — (pip) | no | good | Free Microsoft neural voices, 100+ locales. Unofficial endpoint: no SLA, check the terms. |
| `stub` | — | — | — | A tone. Right length, right place, no words. |

Voices are assigned per speaker in the order you give them:
`--voice-pool anushka,abhilash`, or explicitly with
`--voice-map SPEAKER_00=anushka,SPEAKER_01=abhilash`.

## Separation (`--separator`)

| name | needs | notes |
|---|---|---|
| `demucs` | `pip install demucs` | `htdemucs` two-stem. Convincing removal, needs disk for weights and CPU time. |
| `ffmpeg_center` | nothing | Mid/side. Instant. Keeps a stereo bed and most of the dialogue is gone — but some bleeds through, and mono sources cannot be split at all (returns no bed, with a warning). |
| `none` | nothing | The original audio becomes the bed, dialogue included. Only right for clean narration. |

## Lip sync (`--lipsync`)

| name | env | notes |
|---|---|---|
| `none` | — | Default. The picture is untouched. |
| `synclabs` | `SYNCLABS_API_KEY` | Sync-2. Needs public HTTP(S) URLs for both inputs; dubforge will not upload your footage for you. Quote-based pricing. |

## Writing a provider

```python
from dubforge.providers.base import Spec, TTSProvider, SynthResult

class MyTTS(TTSProvider):
    spec = Spec(
        name="mine", kind="tts", title="My voice engine",
        env=("MYTTS_API_KEY",),
        capabilities=("style",),
        languages="en, hi",
        verified=False,
        quality="Fast and cheap; flat prosody on long sentences.",
        docs="https://example.com/docs",
    )

    def voices(self, language=""):
        return []

    def synthesize(self, text, voice, out_path, style=None, emotion=None, sample_rate=48000):
        # write a wav to out_path, return SynthResult(path=..., duration=...)
        ...
```

Then one line in `providers/registry.py:REGISTRY["tts"]`, optionally one in `AUTO`.
The test suite will insist on a title and a quality note, and will check that no spec
carries a secret.

Rules that are not negotiable in an adapter:

* never log or return a key;
* raise `ProviderError` with a message a user can act on, never a bare traceback;
* retry 429/5xx, never retry a 400 (a bad request will stay bad);
* return a measured duration when you can, and `None` when you cannot.
