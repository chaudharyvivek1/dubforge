# Changelog

All notable changes. Dates are ISO. This project follows semantic versioning.

## [0.1.0] — 2026-11-16

First public release. The pipeline is complete and the offline path is fully tested;
network adapters are written against vendor documentation and marked
`verified: false` until they have been exercised against live APIs.

### Added

* **Pipeline** — probe → separate → transcribe → translate → fit the clock →
  synthesise → assemble → normalise → duck and mix → mux → QC, with a resumable work
  directory and a transcript cache keyed by file fingerprint.
* **Duration-aware translation** — per-language characters-per-second model, a window
  per line derived from the next line's start, and a rewrite loop that sends over-long
  lines back to the translator with an explicit character budget.
* **Voices** — one voice per speaker, assigned once and verified in QC; `--voice-pool`
  and `--voice-map`.
* **Bed preservation** — `demucs` two-stem separation, an instant mid/side fallback,
  sidechain ducking, and a warning when no bed could be produced.
* **QC** — ten checks with `fail`/`warn` severities, a metrics block, `qc.json`, a
  one-screen summary, and a non-zero exit code on failure.
* **Subtitles** — source, target, bilingual SRT and WebVTT on every run.
* **Providers** — 7 recognisers, 6 translators, 6 voices, 3 separators, 2 lip sync,
  each with a spec, a capability list and an honest quality note.
* **CLI** — `doctor`, `providers`, `voices`, `plan`, `dub`, `demo`, `srt`, `mcp`.
* **MCP server** — ten tools, instructions that encode the correct order of operations,
  and transcripts returned as paths rather than payloads.
* **Agent skill** — `skill/SKILL.md` plus provider, quality, prompt and troubleshooting
  references.
* **Docs** — architecture, providers, quality, languages, cost, school deployment,
  platforms, security and consent.
* **Tests** — 56 tests, including an end-to-end dub with real ffmpeg, and one that
  asserts a stub run is reported as a failure.

### Known limitations

* Network adapters are unverified against live APIs in this release.
* No HTTP server: `dubforge.dub()` is a library, and wrapping it is left to the
  deployer on purpose.
* Lip sync needs public URLs and does not upload for you.
* Speech-rate constants are empirical defaults, not measured per language.
