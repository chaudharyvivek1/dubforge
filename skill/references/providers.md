# Provider choices for an agent

Read `list_providers` first — it is the live truth for the machine you are on. This
page is the judgement that goes on top of it.

## Recognition

| Situation | Pick | Why |
|---|---|---|
| Interview, panel, drama — more than one voice | `deepgram` | diarisation and the best word timings; without it everything is one speaker |
| Accents, names, noisy field audio | `elevenlabs` (Scribe) | strongest on messy speech; flags laughter and music |
| Long footage, tight budget | `groq` | whisper-large-v3-turbo at ~$0.0007/min |
| Must stay offline | `faster_whisper` | local; `--asr-model small` is the accuracy/speed knee |
| A human already transcribed it | `transcript` | free, instant, and better than any model |
| Single clean narrator, no budget | `faster_whisper` or `groq` | diarisation adds nothing for one voice |

**Never** run a multi-speaker video through a non-diarising recogniser and then hand
out voices — every line will be one person.

## Translation

| Situation | Pick | Why |
|---|---|---|
| Timing matters (it always does) | `openai` or `gemini` | they obey a character budget, so the rewrite loop works |
| Indian language, idiom matters | `sarvam` for the first pass, an LLM for the rewrite | Mayura knows the idiom; it cannot shorten on request |
| European pairs, cost per character | `deepl` | excellent fluency, no duration control |
| Nothing may leave the machine | `ollama` | `qwen2.5:14b-instruct` is a reasonable default |
| You only need a timing test | `stub` | it does not translate; QC will fail the run, which is correct |

## Voice

| Situation | Pick | Why |
|---|---|---|
| Must sound like the original person | `elevenlabs` + a cloned voice | only path that preserves identity — needs written permission |
| Indic language, natural prosody | `sarvam` (Bulbul v2) | handles Devanagari, Tamil, Telugu, Bengali script properly |
| Free, decent, no key | `edge_tts` | 100+ locales; unofficial endpoint, no SLA |
| Steerable delivery, cheap | `openai` `gpt-4o-mini-tts` | `style` becomes an `instructions` string |
| Specific locale voice | `google` with a full name like `hi-IN-Neural2-A` | wide locale coverage |

Voice ids go in `voice_pool` (comma separated, assigned to speakers in order) or
`voice_map` (`SPEAKER_00=id,SPEAKER_01=id`) when you know who is who.

## Separation

`demucs` when the music and effects matter and you have the CPU time. `ffmpeg_center`
when the source is stereo broadcast material and you need it now. `none` for clean
narration — and be aware that `none` means the original dialogue is still in the bed,
under the new one.

## Lip sync

Off by default. Turn it on only when the user asks for it explicitly, and tell them:
it needs public URLs for both files, it reshapes real mouths, and it costs more than
the whole rest of the pipeline. For a talking-head presenter it can look convincing;
for fast cuts and occlusions it does not.

## A default stack that is hard to beat

```
asr        deepgram        (diarisation + timings)
translator openai          (obeys the character budget)
tts        sarvam          (Indic) or elevenlabs (voice identity)
separator  demucs
lipsync    none
```

Cost for 10 minutes: about $0.75 with ElevenLabs voices, about $0.10 with Sarvam, and
about $0.03 with `edge_tts`. See `docs/cost.md` in the repository for the list prices
behind those figures.
