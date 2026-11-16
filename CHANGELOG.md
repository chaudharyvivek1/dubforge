# Changelog

All notable changes. Dates are ISO. This project follows semantic versioning.

## [0.1.2] — 2026-11-18

Second audit: concurrency, configuration and release hardening. No public API, CLI
command or MCP tool name changed.

### Fixed — concurrency

* **The MCP transcription path released its workspace lock before saving (high).**
  `transcribe_video` held the lock only until the provider returned, so a second request
  could claim the same directory and overwrite the first one's transcript — silently, and
  with a valid-looking file at the end. The lock is now held through extraction,
  transcription, the save and the payload build, and released by a context manager on
  any exception, including a failed save. The write path moved to
  `server.run_transcription()` so it can be driven from several threads in tests.
* **Transcript saves were not atomic (medium).** `Transcript.save()` wrote in place; a
  crash could leave a half-written file that the next run would refuse. It now goes
  through the same temp-file + `os.replace` path as every other write.

### Fixed — configuration

* **A malformed boolean was silently `False` (high).** `"flase"`, `"maybe"` and `"2"`
  all parsed as `False`, quietly switching off caching, background preservation or
  ducking. Only `1/true/yes/on` and `0/false/no/off` are accepted now, case-insensitively;
  anything else is a warning and the default is kept.
* **Configuration files could fail obscurely (medium).** `Settings.load()` used to raise
  a raw `JSONDecodeError`, accept a non-object root, ignore unknown keys (a typo like
  `targt_lang` meant the wrong language, silently), accept `NaN`/`Infinity`, and let a
  wrong type surface as a `TypeError` three frames deeper. Every case is now a
  `ConfigError` naming the file, the key and what was expected, and the CLI prints it
  instead of a traceback.

### Fixed — cache locking

* **The cache lock fell back to an unlocked write (high).** After a timeout, `_save_cached`
  wrote anyway, which is exactly how two writers corrupt one file. It now raises
  `CacheWriteError`; the pipeline treats the cache as optional, records a warning and
  carries on with the transcript it already has. The lock gained stale-lock recovery
  (dead pid on POSIX, age on Windows, where probing a pid is unsafe) and a 10 s wait.

### Added

* **Windows CI.** A `windows-latest` / Python 3.12 job runs lint, the full suite, the
  concurrent workspace and MCP tests on their own, the offline demo gate, MCP startup and
  tool registration, and a Windows-specific workspace check (locking, isolation, stale
  lock reclaim, cleanup).
* **`docs/assessment.md`** — the architecture, feature and gap inventory, dependency and
  licence assessment, security risk assessment and the phased roadmap, written before the
  next phase of work rather than after it.

### Fixed — Windows (found by the new Windows job)

* **Concurrent cache writes failed on Windows (high).** The file lock cannot separate two
  *threads* in one process — they share a pid, so a lock held by a sibling thread looked
  live forever and every other writer waited out its timeout. On Windows the release also
  failed with `PermissionError` when another thread had the lock file open. There is now
  an in-process lock per cache directory around the file lock, and deletion/replace go
  through a short retry (`unlink_retry`, and `os.replace` retried inside
  `atomic_write_text`), which is what Windows requires when a file is momentarily open.
  The first Windows run failed on exactly this; it is fixed and re-run, not skipped.

* **`--json` output died on a legacy console (high).** A single `→` in a step detail
  made `json.dumps`/`print` raise `UnicodeEncodeError` on a Windows code page: the
  command exited **1** — the same code as "QC failed" — with empty stdout, so the CI
  gate read a crash as the expected outcome. Machine output is now pure ASCII
  (`ensure_ascii=True`), the console is reconfigured to UTF-8 with
  `errors="replace"` for human output, the gate refuses an exit 1 that came with a
  traceback or empty stdout, and every gate failure prints the stderr tail.

### Tests

232 (was 143). New: two simultaneous transcriptions of one source, same-named files in
different folders, provider failure, save failure, "the lock is held during the write",
sequential reuse, every boolean field against every documented word, six invalid boolean
values, twelve invalid configuration files, cache lock blocking, stale-lock recovery,
dead-pid recovery, an unwritable cache directory, and a cache failure that must not fail
the dub, a thread holding the cache lock blocking siblings without hanging, and six
threads serialising their writes, a crash that exits 1 not being mistaken for the
expected failure, and the demo run under `PYTHONIOENCODING=cp1252` still emitting valid
ASCII JSON.

### Known limitations

* No job lifecycle, progress or cancellation yet — see `docs/assessment.md` §4.1.
* Network adapters remain `verified: false`.
* No limits on input size or duration.

## [0.1.1] — 2026-11-17

Production audit release. No public API, CLI command or MCP tool name changed; the
working architecture is untouched. Two behaviours *did* change on purpose, and both are
documented below.

### Fixed — correctness

* **Cache key could collide (high).** `file_fingerprint` used filename + size + integer
  mtime, so two different videos with the same name, size and timestamp shared a
  transcript — and a re-encode that kept its size reused the old one. Keys are now
  SHA-256 of the file's contents, read in 1 MiB chunks, plus the ASR provider, model,
  source language and diarisation flag. The filename is deliberately excluded, so
  renaming or moving a file no longer costs a transcription.
* **Cache writes were not atomic or serialised (high).** A crash mid-write left a
  truncated JSON document that every later run treated as a hard failure, and two
  concurrent jobs could interleave writes. Writes now go through a temp file plus
  `os.replace`, guarded by a lock with a 30 s timeout; a corrupt or outdated entry is
  treated as a miss.
* **Workspaces were named after the file stem (high).** Two lessons called
  `lesson.mp4` in different folders shared a work directory and overwrote each other's
  audio; two concurrent dubs did the same. Jobs now live in
  `<workdir>/jobs/<stem>-<pathhash>/`, take an exclusive lock, get `-2`, `-3` … when
  another live job holds the directory, and reclaim a lock left by a dead process.
* **`DUBFORGE_SPEECH_CPS` arrived as a string (medium).** Fields typed `float | None`
  default to `None`, which told the old coercer nothing, so `12.5` became `"12.5"` and
  broke the timing arithmetic. Such fields are now parsed as JSON. A malformed variable
  is ignored **and reported** (`Settings.env_warnings`, shown by `doctor`) instead of
  being swallowed.
* **A provider returning empty audio looked like success (high).** A 200 with a
  zero-byte body produced a segment with a 0 s duration, a hole in the timeline, and a
  dub that passed timing checks. Renders under 128 bytes or 0.02 s are now a named
  failure for that line, and QC gained `dub_is_audible`.
* **The wrong audio track could be transcribed (medium).** `extract_audio` let ffmpeg
  pick the "best" stream; on a multi-track file that is not necessarily the one the
  picture plays. It now maps `0:a:0` explicitly, and QC warns when the source had more
  than one audio track.
* **Output could be the source file (medium).** The mux failed at the *last* step,
  after every provider had been paid, with "ffmpeg cannot edit existing files in-place".
  It is now refused before the first API call.
* **`duck_and_mix` could clip (medium).** The dub is normalised to a true-peak ceiling,
  but the bed is added on top, so the sum could exceed 0 dBFS. A limiter
  (`alimiter=limit=0.95:level=false`) closes the graph.
* **`--no-duck` produced an invalid filter graph (medium).** An unused `asplit` output
  made ffmpeg refuse the mix; the graph is now built per branch.
* **`edge_tts` failed inside the async MCP server (medium).** `asyncio.run()` raises
  when a loop is already running. Coroutines now go through `run_coroutine()`, which
  uses a short-lived thread on the running loop.
* **DeepL used the free host for paid keys (medium).** Free keys end in `:fx`; using the
  wrong host returns 403, which reads like a bad key. The host is chosen from the key.
* **Sarvam's glossary rewrote the source (low).** Terms were substituted in the *input*
  before translation, corrupting the sentence being translated. The glossary is now
  enforced on the output.
* **Deepgram's query string was hand-built (low).** Now `urlencode`d.
* **Partial translations were invisible (medium).** When a translator answered for only
  some lines, the rest silently kept the source text. QC gained
  `complete_translation`, and the summary shows the count.

### Fixed — CI

* **`|| true` hid every demo failure (high).** The offline demo is *supposed* to fail QC
  (stub translation, tone voice) and exit 1; the old step could not tell that from a
  crash, empty JSON, a missing mp4, an unplayable container or a silent track.
  `scripts/ci_demo_check.py` now asserts the exit code, the JSON, the file, both
  streams, that the only QC failure is `translated`, and the metrics — and fails with a
  named code otherwise. CI also checks that the MCP server registers all ten tools and
  returns structured errors.

### Fixed — MCP server

* **Structured errors everywhere (high).** Every tool now returns
  `{"ok": false, "error": {"code", "message", "hint"}}` with a stable code
  (`invalid_argument`, `missing_file`, `unsupported_media`, `no_provider`,
  `provider_error`, `io_error`, `internal_error`). No stack traces, no raw exception
  strings, no key material.
* **Argument validation (high).** Paths, media type, language codes, numeric ranges,
  provider names, `glossary`/`voice_map` syntax, transcript files and output paths are
  validated before any work starts, with a hint that says what to do.
* **`transcribe_video` wrote to a shared path (medium).** Two concurrent calls shared
  `asr/input.wav`; it now uses a locked job directory.

### Changed behaviour

* Work directory layout is now `<workdir>/jobs/<stem>-<hash>/` with a shared
  `<workdir>/cache/`. `DUBFORGE_WORKDIR` still works; only the paths inside it changed.
  `config.workdir_for()` remains for compatibility but the pipeline no longer uses it.
* The target subtitle file is named for the language (`lesson.hi.srt`, not
  `lesson.dub.srt`).
* QC grew from ten checks to fifteen (`complete_translation`, `voice_selected`,
  `dub_is_audible`, `frame_rate_preserved`, `multiple_audio_tracks`).

### Tests

134 tests (was 56), including: cache collisions with identical names and sizes, cache
reuse after a rename, corrupt and outdated cache entries, eight concurrent cache
writers, workspace isolation for same-named files, lock reclaim, resume after a crash,
content-change cleanup, cleanup refusing a symlink out of the job, `shell=True` and
`eval` scans, structured MCP errors for twelve bad inputs, an empty TTS response, a
multi-audio-track source, a hot mix that must not clip, and the CI demo gate's six
failure classifications.

### Known limitations

* Network adapters remain `verified: false` — the request shapes follow vendor docs and
  are unit-tested against fake responses, but they have not been run against live APIs
  in this release.
* `synclabs` lip sync still needs public URLs and still does not upload for you.
* The rewrite loop cannot shorten lines with `deepl` or `sarvam`; it records a warning
  and falls back to `atempo`.
* Windows lock liveness is judged by age alone (24 h), because probing a pid is not safe
  there.

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
