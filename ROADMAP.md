# Roadmap

Written as intentions, not promises. The detailed version — feature and gap inventory,
dependency and licence assessment, security risk assessment, and the phased plan with
sizes — is in [docs/assessment.md](docs/assessment.md). Ordered by what most improves a real dub, not by
what is most fun to build.

## Now — the honest state of 0.2.0

* The pipeline is complete and the offline path is fully tested (309 tests, real ffmpeg,
  a real mp4 at the end of `dubforge demo`).
* Jobs, background execution, cooperative cancellation, capabilities, limits, batch
  processing, provider health and opt-in fallback all shipped in 0.2.0.
* 24 adapters exist; the network ones are written against vendor documentation and are
  marked `verified: false` until exercised against live APIs. `health(live=True)` proves a
  credential works; it does not prove the output is good.

## Next

1. **Verify the network adapters.** Run each against a live key on the same 3-minute
   clip, record what came back, flip `verified` to true. This is the single most
   valuable thing anyone can contribute right now, and the checklist is in
   `CONTRIBUTING.md`.
2. **Word-level alignment in the target language.** Today a line is placed at its start
   and stretched to fit; the next step is per-word timing so pauses land where the
   actor paused, not where the sentence boundary was.
3. **Measured speech-rate constants.** `fit.CPS` is empirical. A small corpus per
   language, measured with a real voice, would replace guesses with numbers.
4. **Segment-level regeneration.** Re-render only the lines that changed, from an edited
   `transcript.dub.json`, instead of the whole file. Half the code exists (the cache);
   the other half is a manifest of per-segment audio hashes.

## Later

* **A local TTS adapter** (Piper or Coqui) so the fully-offline path has a voice and not
  just a tone.
* **Multi-track output** — separate dialogue, music and effects stems in the delivered
  container, which is what a broadcaster actually asks for.
* **Emotion from the source.** Estimate per-segment delivery from prosody instead of a
  single global `--emotion` hint.
* **A QA web page** that plays the original and the dub side by side with the bilingual
  subtitle under both. Static HTML, no server.
* **Subtitle burn-in** for the target language, with the same styling knobs as the SRT.
* **Windows path and console hardening** — long paths and code pages are the two things
  that break first on a school laptop.

## Explicitly not planned

* **A hosted service.** dubforge is a library and a CLI. A dubbing service that accepts
  uploads is an upload service, with all the security questions that implies, and those
  answers are specific to each deployment.
* **Training models.** This orchestrates vendors and open models; it does not become one.
* **Circumventing a vendor's consent or licensing rules.** Voice cloning is gated on
  permission by design and stays that way.
