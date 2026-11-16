# Troubleshooting

## Errors you will actually see

**`no ffmpeg`** — no binary on PATH and `imageio-ffmpeg` is not installed.
`pip install imageio-ffmpeg`, or install a system ffmpeg, or set `DUBFORGE_FFMPEG`.
`dubforge doctor` names the missing filters if the build is the problem.

**`this ffmpeg build is missing filters`** — a stripped build (some NAS packages, some
slim images). Install a full build; there is no workaround for `sidechaincompress`.

**`no <stage> provider is ready. Tried: …`** — every candidate is missing something,
and the message names each one. The fix is in the list: set a key, install a package,
or name a provider explicitly with `--asr`/`--translator`/`--tts`.

**`provider 'x' cannot run: environment variable Y is not set`** — you named a provider
that is not configured. That is a feature: it never silently substitutes another one.

**`no speech was found in the audio`** — the recogniser returned nothing: music-only
file, silent track, or a very quiet recording. Check with `probe_media`, and if you
have a transcript, pass `--transcript`.

**`no line could be synthesised. First error: …`** — every TTS call failed. Usually a
bad key, a voice id that does not exist for that language, or a rate limit. The first
error is quoted; fix that one.

**`translator did not return a JSON array`** — the model answered in prose. Retry; if
it persists, the batch is too large or the model is too small (a 7B local model
sometimes refuses JSON). Reduce `--rewrite-rounds`, or switch translator.

**`'deepl' did not shorten the lines it was given`** — expected. DeepL and Sarvam
Mayura cannot shorten on request; atempo handles the timing. Use an LLM translator if
the timing matters.

**`lip sync needs a public URL`** — by design. dubforge will not upload your footage
for you.

**`ffmpeg failed (234) …`** — a filtergraph error, usually a filter that build does not
have, or an odd channel layout in the source. Run `dubforge doctor`; then try
`--separator none` to isolate.

## Symptoms, not errors

**Rushed delivery.** Check `max_speed_applied` in QC. If it is at the cap, the
translator was not shortening enough: raise `rewrite_rounds`, or lower `max_speed` so
more lines go back for rewrite.

**Lines land late and drift.** A line overran and the assembler let it overlap the next
one. Look at `worst_overflow_s` and the segment with the biggest overflow in
`transcript.dub.json`, then split or shorten that line.

**Everything in one voice.** The recogniser did not diarise. `--asr deepgram`, or pass
`voice_map` if you know the speakers.

**The music is gone.** No bed was produced: `background_kept` will be false with a
reason. Install `demucs`, or check that the source is actually stereo for
`ffmpeg_center`.

**The music fights the voice.** Lower `--bed-gain-db` to −20, keep ducking on, and
check the bed was separated rather than being the full mix.

**The dub is quieter than the original.** Loudness is normalised to −16 LUFS by
design; the original may be −12. If they must match, `--loudness -12`.

**Garbled or clipped audio.** Check `peak_db`; drop `--loudness` to −18, and check the
voice output was not already distorted.

**Hindi written in Latin letters.** The translator returned transliteration. Say so in
`context` ("write in Devanagari script"), and pin a couple of glossary terms in
Devanagari to force the script.

**Numbers read in English.** Pin them: `--glossary "2026=दो हज़ार छब्बीस"`.
