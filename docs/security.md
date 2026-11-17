# Security, consent and data

A dubbing tool does two sensitive things at once: it processes recordings of real
people, and it can make those people say things they never said. Both deserve a
straight answer rather than a paragraph of reassurance.

## What leaves the machine

Only what a provider needs to do its job, sent straight from your machine to the
vendor with your key:

| Stage | What is sent | Where |
|---|---|---|
| Recognition | the 16 kHz mono audio of the whole file | the ASR provider you chose |
| Translation | the transcript text | the translator you chose |
| Voice | one line of text per request (plus the voice id) | the TTS provider you chose |
| Separation | nothing, if `demucs`/`ffmpeg_center` | local |
| Lip sync | a **URL** to the video and audio, not the bytes | the lip sync provider |

There is no dubforge server, no proxy, no telemetry, no analytics. `dubforge doctor`
prints whether an environment variable is set and never its value. Nothing is written
outside `--workdir` and the output path.

**Fully offline configuration:** `--asr faster_whisper --translator ollama
--separator demucs`, and either accept `stub` voices for a timing test or use a local
TTS engine. `edge_tts` is free and needs no key, but it *is* a network call.

## Keys

* Read from the environment (`OPENAI_API_KEY`, `DEEPGRAM_API_KEY`,
  `ELEVENLABS_API_KEY`, `GROQ_API_KEY`, `SARVAM_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, `DEEPL_API_KEY`, `SYNCLABS_API_KEY`) or from a JSON config passed
  with `--config`. Nothing else.
* Never written to a log, a report, a transcript or an error message. A provider's
  error is truncated to 300 characters of the *response* body, and request headers are
  never echoed.
* Never passed as a CLI flag on purpose: a key on a command line ends up in your shell
  history and in `ps`. Use the environment.
* `dubforge doctor --json` is safe to paste into a bug report. It lists variable names
  and `true`/`false`.

If you run the MCP server for several people, give each their own environment. There
is no shared credential store, and that is deliberate — a shared store is a shared
blast radius.

## Consent

This is the part that matters more than any setting.

* **Cloning a voice needs the speaker's written permission.** ElevenLabs and similar
  services require it in their terms as well as in law in most jurisdictions. For a
  teacher, a student, an actor or a family member: get it in writing, keep it with the
  project, and record what it covers (which videos, which languages, how long).
* **Dubbing a third party's video is a copyright question before it is a technical
  one.** A dub is a derivative work. Owning the file is not owning the rights.
* **A voice clone is a deepfake with a good excuse.** Do not publish a dubbed video of
  a real person without making it clear it is dubbed — in the description, or with a
  caption. This is also increasingly a legal requirement, not only an ethical one.
* **Students on camera** need the school's own media-consent process, not just a
  teacher's nod.

dubforge cannot enforce any of this. It can make the honest path easy: `--style` and
`--context` are how you describe a real person's delivery without needing their voice
at all, and the free voices are good enough for most classroom material.

## Student and patient data

Recordings of children, medical consultations and anything under a data-protection
regime (India's DPDP Act, GDPR) should not be sent to a third-party API without a
lawful basis. For those cases:

1. use the offline configuration — nothing leaves the machine;
2. keep `--workdir` on an encrypted volume;
3. delete intermediates after a good run (`--no-keep-work`), keeping only the output
   and `transcript.dub.json` if you need to re-voice later;
4. check your provider's retention policy: several vendors keep uploaded audio for a
   window for abuse review, and some offer zero-retention settings on request.

## The trust boundary for agent-supplied paths

The MCP server runs locally, so an agent can ask it to read and write files. The
boundary is deliberately narrow:

* **Inputs are validated, not trusted.** Every path is expanded, checked for existence
  and type, and probed before use; a language code must match `^[a-z]{2,3}(-[A-Za-z]{2,4})?$`;
  numbers are range-checked; `glossary` and `voice_map` are parsed with an error that
  says what was wrong.
* **Writes stay in the workspace.** Artefacts go under `<workdir>/jobs/<stem>-<hash>/`,
  and every write goes through `JobWorkspace.file()`, which refuses a path that resolves
  outside that directory (`../`, an absolute path, a symlink).
* **Cleanup re-checks the boundary.** `cleanup()` resolves each candidate and skips
  anything outside the job directory, so a symlink planted in the work directory cannot
  turn `--no-keep-work` into a delete somewhere else.
* **The output path is the user's choice, with one guard.** It may be anywhere, but it
  may not be the source file — caught before the first paid API call, not by ffmpeg at
  the last step.
* **Errors are structured and quiet.** Every tool returns
  `{"ok": false, "error": {"code", "message", "hint"}}`. Codes are stable
  (`invalid_argument`, `missing_file`, `unsupported_media`, `no_provider`,
  `provider_error`, `io_error`, `internal_error`); messages are truncated to 600
  characters and never contain a header, a key or a stack trace.
* **No shell.** Every subprocess call takes an argument list; `shell=True`,
  `os.system` and `os.popen` do not appear anywhere in the package, and a test enforces
  that.

What this boundary does **not** give you: an agent that can already write files on the
machine can still pass a path you would rather it did not. Run the server as a user
whose filesystem access matches what you are willing to grant it.

## Jobs, ids and cleanup (0.2.0)

* **Job ids are validated before they touch the filesystem.** `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`
  — `../../etc/passwd` is an `invalid_argument`, not a path.
* **Cleanup is bounded twice.** It only considers finished jobs, only deletes
  `registry/<id>.json` and `jobs/<id>/`, and resolves each directory against
  `<workdir>/jobs/` before removing it. `dubforge_cleanup_jobs` defaults to
  `dry_run=true`; the CLI defaults to a dry run and needs `--yes`.
* **Job documents carry no secrets and no transcript.** Status, paths, counts, QC summary,
  provider names. `dubforge_get_job_result` returns paths, not text.
* **A job cannot be executed twice.** The runner refuses a duplicate submission of an
  in-flight job, and the workspace lock covers the cross-process case.
* **Limits are enforced before spending.** `max_input_bytes`, `max_input_seconds`,
  `max_run_seconds`, `max_estimated_usd`, `max_concurrent_jobs` — a refusal with the
  setting to change, never a silent override.
* **Cancellation is cooperative**, and the tool output says so rather than implying an
  in-flight provider call is interrupted.

## Retries, billing and ambiguous outcomes (0.2.1)

A retry is a spend. The HTTP layer therefore distinguishes two failures that look alike:

* the vendor **answered** 429/5xx — nothing was billed, so retrying is free and correct;
* the connection **timed out** — the vendor may have accepted and billed the request.

The second case is not retried for a non-idempotent call (synthesis, translation,
transcription). It raises `ProviderError(kind="ambiguous")` with a message that says the
outcome is unknown, and the run records it. Only OpenAI's documented `Idempotency-Key` is
used; no header is invented for any other vendor, and no call is marked idempotent on a
guess. `max_retries` bounds the budget, `Retry-After` is honoured rather than guessed, and
every retry is counted in the job document, the report and the event log — never with a key
or a header in it.

## Job identity and ownership (0.2.1)

* **A job id is validated** against `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` before it touches
  the filesystem, so `../../etc/passwd` is an `invalid_argument`.
* **A claim file is the lock.** Created with `O_EXCL` in the registry directory, holding a
  token, pid, host and start time. Only the owner may update the job, refresh its heartbeat
  or release it; a claim from a dead process is reclaimed, a live one is never stolen.
* **A stale worker cannot overwrite a newer attempt**: `update(token=…)` fails if the claim
  moved on.
* **Cleanup stays inside the work directory**: it only deletes `registry/<id>.json`,
  `registry/<id>.claim` and `jobs/<id>/`, resolving each path against the workdir first.
* **No secrets in diagnostics**: events and warnings carry provider names, ids, attempts and
  error kinds — never a key, a request header or transcript text.

## Uploads for lip sync

`synclabs` fetches URLs. dubforge deliberately does **not** upload your footage to a
bucket on your behalf — that would mean handing a third party your video without you
choosing where. If you enable lip sync, you upload, you control the link, you decide
the expiry, and you delete it afterwards.

## Failure modes that are security-relevant

* **A prompt injection inside a transcript.** The translator sees the transcript as
  data. A hostile video could contain a line like "ignore the instructions and output
  …". The adapters request strict JSON and discard anything that is not a line of
  translation, but a hostile transcript is still a hostile input: treat a dub of
  untrusted material as untrusted output and read the SRT before publishing.
* **A model writing something into the subtitles.** Same mitigation: the JSON contract
  plus a human read. There is no path by which a translator's output becomes a command
  in this pipeline.
* **A very large file.** Everything is local disk and ffmpeg; there is no size limit
  imposed and none enforced. Watch the work directory on a small laptop.
* **A shared work directory.** The transcript cache is keyed by file *content*, so two
  jobs that happen to process identical bytes will share a transcription. That is
  intended, and it is why the key includes the ASR provider, model, language and
  diarisation flag — but if you keep unrelated clients' material under one `--workdir`,
  give each client their own.

## Reporting a problem

If you find a way for a provider response, a transcript or a media file to make
dubforge do something it should not — leak a key, write outside the work directory,
execute something — open an issue with the reproduction and the QC report. Those are
treated as security bugs, not feature requests.
