# Quality

A dub is not a translation with a voice on top. It is a performance that has to land
inside the time the original mouth was moving, in a voice that stays the same person,
over music that is still there. This page is the arithmetic and the checks behind
that sentence.

## 1. The clock

Everything starts with one number per line: its **window** — the seconds between the
line's own start and the start of the next line, minus `--tail-gap` (0.12 s) so two
voices never collide.

```python
window[i] = next.start - tail_gap - segment[i].start
window[last] = total_duration - start          # the tail is available
```

A line is allowed to run into the silence after it. It is never allowed to push the
next line later, because the picture does not move.

### Estimating spoken duration

```
estimated_seconds = characters / cps(language)
```

`cps` is characters per second of comfortable narration. The defaults are empirical,
not measured on your footage, and they are overridable:

| language | cps | language | cps |
|---|---|---|---|
| en | 15.0 | ta | 11.5 |
| hi, mr | 13.5 | te, kn, ml | 11.5 |
| bn | 12.5 | ur | 12.5 |
| gu, pa | 13.0 | es | 15.5 |
| or, as | 12.5 | fr | 14.5 |
| de | 13.5 | ja | 8.5 |
| ar, fa | 12.5 | zh | 7.0 |

Devanagari, Tamil and Arabic carry more meaning per character than Latin, and
Japanese and Chinese carry far more per character — budgeting them like English is
how a Tamil dub ends up 30% too long.

### The three verdicts

| ratio | verdict | what happens |
|---|---|---|
| ≤ 1.00 | `ok` | nothing. A short line gets a pause, which is what actors do. |
| 1.00 – `--max-speed` | `stretch` | `atempo`, pitch preserved, capped at 1.15× by default |
| > `--max-speed` | `rewrite` | sent back to the translator with a character budget |

`atempo` is not free. 1.1× is inaudible; 1.25× starts to sound rushed; 1.4× sounds
like a different person. That is why the default cap is 1.15 and why the rewrite pass
exists: **asking for a shorter sentence sounds better than compressing a long one**,
every time.

### The rewrite loop

```
for round in range(--rewrite-rounds):        # default 2
    over = [s for s in segments if verdict(s) == "rewrite"]
    if not over: break
    translator.translate(over, instructions={s.index: constraint_note(s) for s in over})
```

`constraint_note` gives the model the numbers, not an adjective:

> This line must be spoken in 2.4s (about 41 characters in hi). The current
> translation is 63 characters, roughly 4.7s. Rewrite it shorter: keep the meaning
> and the speaker's intent, drop filler and repetition, prefer shorter synonyms. Do
> not drop a fact the line exists to carry.

If the translator cannot shorten (DeepL, Sarvam Mayura), the loop stops after one
round, records a warning, and atempo does what it can.

## 2. The voice

* **One voice per speaker**, assigned once by `timeline.map_voices`, held for the
  whole file, verified in QC. Two voices for one character is the most common defect
  in cheap dubs and it is entirely avoidable.
* **Diarisation first.** Without speaker labels every line becomes `SPEAKER_00` and
  the whole film gets one voice. Use `deepgram` or `elevenlabs` (both diarise), or
  `--script` with `SPEAKER_00:` prefixes for the stub.
* **Emotion** goes to the vendors that accept it: `--style` maps to ElevenLabs
  `voice_settings.style` and to OpenAI's `instructions`. `--emotion` is the same
  channel with a different intent. Neither is a substitute for `--context` reaching
  the translator, which changes the words rather than the delivery.

## 3. The bed

`--separator demucs` removes the dialogue and keeps the music and effects. The bed is
mixed at `--bed-gain-db` (default −16 dB) and **ducked** by `sidechaincompress` keyed
from the dub: it dips while someone talks and returns in the pauses. `--no-duck` for
material where the bed is already quiet; `--no-background` to replace the soundtrack
entirely (right for a clean voice-over, wrong for anything with a score).

Mid/side (`ffmpeg_center`) works because dialogue is centre-panned. It fails on mono,
and it leaks some original dialogue. It is the fallback, not the target.

## 4. Loudness and headroom

The dub is normalised to `--loudness` (−16 LUFS by default) with a true peak ceiling
of −1.5 dBTP, then mixed. −16 LUFS is what streaming platforms normalise to and loud
enough to survive a phone speaker. −14 for podcast, −23 for broadcast EBU R128.

## 5. The checks

`qc.json`, printed by `dubforge dub` and in `report.md`. `fail` exits non-zero.
Twenty-three checks for a single-audio-track source (twenty-four when the source has
several audio tracks, because `multiple_audio_tracks` is only emitted then), because the
failures are independent: a dub can be perfectly timed,
correctly voiced, loud enough — and silent, if the voice provider answered 200 with
nothing in the body.

| check | severity | passes when |
|---|---|---|
| `every_line_has_audio` | fail | every segment has a rendered file, longer than 0.02 s |
| `translated` | fail | no segment was touched by the stub translator |
| `complete_translation` | warn | the provider answered for every line (not just some) |
| `no_empty_lines` | fail | no empty translation |
| `timing_fits` | fail | no line overruns its window by more than 0.35 s |
| `speech_rate` | fail | no line exceeded `--max-speed` |
| `one_voice_per_speaker` | fail | each speaker used exactly one voice |
| `voice_selected` | warn | a voice id was chosen for every line, not the provider default |
| `dub_is_audible` | fail | the dub track exists, is longer than 0.05 s and peaks above −60 dBFS |
| `headroom` | warn | peak ≤ −0.5 dBFS |
| `loudness` | warn | within ±2.5 LU of the target |
| `duration_preserved` | warn | output within 0.5 s of input |
| `frame_rate_preserved` | warn | the copied video stream kept its fps |
| `subtitle_timing` | fail | every subtitle has a positive duration and consecutive cues do not overlap |
| `subtitle_readability` | warn | lines fit `qc_max_chars_per_line` (42), at most `qc_max_lines` (2), under `qc_max_reading_cps` (20) and on screen for `qc_min_display_s` (0.8) |
| `streams_present` | fail | the output has the dubbed audio and, if the source had one, a video stream — and exactly one audio stream |
| `video_codec_preserved` | warn | the picture was streamed through, not re-encoded |
| `audio_layout` | warn | the dub is at least 44.1 kHz with one or two channels |
| `not_mostly_silent` | warn | less than 75% of the dub track is silence, which is how a missing line shows up |
| `multiple_audio_tracks` | warn | *conditional*: emitted only when the source has more than one audio stream — the first was dubbed, the others were not |
| `short_tts_output` | warn | every render is at least `qc_min_tts_ratio` (30%) of its text's estimated duration — a far shorter render usually means the provider truncated |
| `excessive_compression` | warn | no line played faster than `qc_max_audible_speed` (1.10×), above which atempo is audible as compression even when the timing is legal |
| `silence_inside_line` | warn | no rendered line is more than `qc_line_silence_ratio` (50%) silence; measured on at most `qc_silence_sample` (40) lines so a long film does not cost 900 decodes |
| `background_kept` | warn | a bed was mixed under the dub |

`fail` means *do not ship*. `warn` means *listen to this one*. A run that ends with
`ok: true` has cleared the mechanical checks; it has not been listened to, and the
bilingual SRT exists so that a human can check the words in a couple of minutes.

## 6. Tuning a dub that does not sound right

| symptom | likely cause | fix |
|---|---|---|
| rushed, breathless lines | translator ignores the budget | use an LLM translator, raise `--rewrite-rounds`, lower `--max-speed` to 1.08 |
| lines land late, drift over the film | a line overran and pushed the rest | check `timing_fits`; shorten that line, or split the segment |
| same voice for everyone | recogniser did not diarise | `--asr deepgram`, or label with `--script` |
| music gone | no bed | install `demucs`; check `background_kept` |
| music fights the voice | bed too hot | lower `--bed-gain-db` to −20, keep ducking on |
| names pronounced wrong | translation, not TTS | `--glossary "ISRO=इसरो"`, or write it phonetically in the target script |
| flat, robotic delivery | no style hint, or a cheap voice | `--style "warm, unhurried documentary narrator"`, or a better voice |
| clipped, distorted | headroom | check `peak_db`; lower `--loudness` to −18 (the mix already limits at −0.45 dBFS) |
| a gap where a line should be | a provider returned empty audio | `dub_is_audible` / the line's warning names it; re-run that line |
| dub sounds like a phone call | sample rate / bitrate | keep 48 kHz; raise `--audio-bitrate` to 256k |

## 7. What QC cannot tell you

Whether it sounds like *the same person*, whether the joke is still funny, whether
the register suits a school assembly. Those are listening judgements. The most
efficient review loop this tool can offer is: open `*.bilingual.srt`, read the
translation against the original, then watch two minutes at the start, the middle and
the end.
