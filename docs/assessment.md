# Assessment: architecture, features and roadmap

Written before the next phase of work, as the project's own rules require: inspect,
report, then implement in separate reviewed changes. **Every row below states the release it
shipped in, or that it is still missing.** Where a feature is partial, it says exactly how
far it goes.

Current release: **0.8.0**. Everything in "implemented" is tested; everything else is
not, and is not advertised anywhere.

---

## 1. Current architecture

```
CLI (cli.py)            MCP server (server.py)         Library (__init__.py)
        \                       |                             /
         \                      |                            /
          └──────────── pipeline.plan() / pipeline.dub() ───┘
                                  |
   ┌───────────┬──────────────┬───┴────────┬──────────────┬─────────────┐
   │           │              │            │              │             │
 ffmpeg.py   fit.py       timeline.py    qc.py         srt.py      workspace.py
 (media)   (timing maths) (segments)   (checks)     (subtitles)   (jobs, locks, cache)
   │                                       │
   └─────────────── providers/ ────────────┘
        base.py  registry.py  asr.py  translate.py  tts.py  separate.py  lipsync.py  stub.py  http.py
```

| Module | Responsibility | Notes |
|---|---|---|
| `pipeline.py` | the eleven steps, `plan()` and `dub()`, cache read/write | the only module that knows the order |
| `ffmpeg.py` | every decode, filter, mix, mux, measurement; binary resolution | no shell, ever; args are lists |
| `fit.py` | characters-per-second model, per-line windows, rewrite budget | the dubbing-specific maths |
| `timeline.py` | words → sentences → speakers → windows → voice map | deterministic, fully unit-tested |
| `media.py` | `Word` / `Segment` / `Transcript`, JSON round-trip, content hash | the data model everything shares |
| `qc.py` | twenty-three checks, metrics, one-screen summary | `fail` exits non-zero |
| `srt.py` | source / target / bilingual SRT, VTT, SRT import | review artefacts |
| `workspace.py` | job directories, locks, manifest, shared cache, safe cleanup | added in 0.1.1 |
| `config.py` | `Settings`, environment and JSON file parsing, validation | added strict booleans and file validation in 0.1.2 |
| `doctor.py` | runtime diagnosis with per-platform fixes | never prints a key |
| `pricing.py` | indicative list prices with sources | estimates, not quotes |
| `providers/*` | 24 adapters behind 5 interfaces + a registry | `verified` flag is honest |
| `demo.py` | the offline clip and the offline run | CI's only end-to-end test |

**Invariants** (enforced by tests, not by convention):

1. Paths cross module boundaries, never payloads.
2. Identity of a media file is its SHA-256, never its name or mtime.
3. One job, one locked directory; a destructive operation re-checks its boundary.
4. A failed check never reports success: QC decides, and the CLI exits 1.
5. No secrets in logs, reports, manifests, errors or tool payloads.
6. The video stream is never re-encoded.

**Where it is weakest.** The pipeline is synchronous and single-process. There is no job
queue, no cancellation, no progress channel, and no way to ask "what happened to the run
I started ten minutes ago" other than reading the work directory. That is the gap the
next phase closes (see §5).

---

## 2. Feature inventory — implemented

| Area | Feature | Where | State |
|---|---|---|---|
| Dubbing | probe, decode, separate, transcribe, translate, fit, synthesise, assemble, normalise, mix, mux, QC | `pipeline.py` | stable |
| Timing | per-language CPS, per-line windows, rewrite loop, atempo with a cap | `fit.py`, `timeline.py` | stable |
| Speakers | diarisation from the provider, one voice per speaker, `--voice-map` | `timeline.py`, `providers/asr.py` | stable |
| Voice | per-speaker assignment, style/emotion hints where the provider accepts them | `providers/tts.py` | stable |
| Bed | demucs / mid-side separation, sidechain ducking, limiter | `providers/separate.py`, `ffmpeg.py` | stable |
| Translation | 5 providers + offline stub, glossary pinning, context brief | `providers/translate.py` | stable |
| Subtitles | SRT/VTT write, bilingual SRT, SRT import (`--transcript x.srt`) | `srt.py` | stable |
| QC | 15 checks, metrics, `qc.json`, `report.md` | `qc.py` | stable |
| Jobs | directory per source path, lock, manifest, resume, content-keyed cache | `workspace.py` | stable |
| Config | env + JSON file, strict booleans, validation, actionable errors | `config.py` | stable |
| Diagnosis | `doctor`, `providers`, `voices`, `plan` | `cli.py` | stable |
| MCP | 10 tools, structured errors, argument validation | `server.py` | stable |
| CI | Linux 3.10–3.12, macOS 3.12, Windows 3.12, demo gate, MCP check | `.github/workflows/ci.yml` | stable |
| Docs | 9 documents + skill + FAQ + credits + assessment | `docs/` | stable |

## 3. Feature inventory — missing or partial

| Area | Feature | State | Note |
|---|---|---|---|
| Jobs | job lifecycle, job ids, status, result, cancel, list | **done (0.2.0), hardened (0.2.1)** | request-based ids, claims with tokens, heartbeat, validated transitions |
| Jobs | multi-language jobs from one source | **done (0.2.1)** | the request hash is part of the id |
| Jobs | cross-process ownership | **done (0.2.1)** | claim file with `O_EXCL`; tested with a subprocess |
| Providers | retry safety for non-idempotent calls | **done (0.2.1)** | `ambiguous` instead of a silent second charge |
| Jobs | progress reporting, cancellation, retry of a failed stage | **done (0.2.0)** | cancellation is cooperative; stated, not implied |
| MCP | `dubforge_get_capabilities`, `validate_config`, job tools, `cleanup_jobs`, `check_provider_health` | **done (0.2.0)** | 11 new tools; `analyze_video` deliberately skipped (duplicate of `probe_media` + `plan_dub`) |
| MCP | `translate_transcript`, `generate_speech` as separate stage tools | **missing** | the pipeline, regeneration and the review loop are exposed; a stage tool would need a segment-level contract for the caller |
| Providers | fallback chain when a provider fails mid-run | **done (0.2.0)** | opt-in, announced, recorded in `providers_used` |
| Providers | health check beyond "is the key set" | **done (0.2.0)** | cheap authenticated probe for 7 adapters, honest "no probe" for the rest |
| Providers | a repeatable live-verification process | **done (0.8.0)** | `dubforge verify plan\|run\|import\|status`, validated records, dated `ok` rows; nothing verified live yet |
| Providers | rate-limit aware retry with backoff budget | **done (0.2.0)** | `Retry-After` honoured; `max_retries` configurable |
| Limits | input size, duration, runtime, cost, concurrency, retries, retention | **done (0.2.0)** | `limits.py`, checked before spending and between stages |
| Batch | folder processing, failure isolation, resume, aggregate estimate | **done (0.2.0)** | `batch.py` + `dubforge batch` |
| Audio | multi-track export (dialogue / music / effects in one container) | **missing** | the mix is single-track |
| Audio | per-segment regeneration | **done (0.3.0)** | `segments.py` + `dubforge regen`; reuse is per line, the mix is rebuilt |
| Audio | partial remixing (splice changed lines into the previous dialogue track) | **done (0.5.0)** | one pass; refused when a changed line overlaps another |
| Audio | reuse of the whole mix and mux when nothing changed | **done (0.6.0)** | content-addressed keys; a no-op run is byte-identical and ~2× faster on the demo |
| Audio | per-region loudness so a splice can reuse the mix outside the edited windows | **decided against (0.7.0)** | measured: it saves at most one pass and moves a global gain to a local one; see `docs/architecture.md` |
| Ops | explain a finished job's cost | **done (0.7.0)** | `dubforge explain`, reading `review.json` |
| Audio | silence trimming, pitch control | **missing** | atempo only, pitch preserved by design |
| Subtitles | line-length limits, reading-speed (CPS) validation | **done (0.2.1)** | `subtitle_readability` warns on both; the text is still whatever the translation is |
| Subtitles | translation memory | **missing** | the glossary is the closest thing today |
| Input | URL ingestion | **missing** | deliberate: it needs SSRF protection first (§7) |
| Limits | maximum input size, maximum duration, per-run budget | **done (0.2.0)** | refused with the setting to change |
| Output | MKV/MOV containers, FLAC/WAV/MP3 audio, configurable bitrate beyond `--audio-bitrate` | **partial** | mp4/m4a only, audio bitrate configurable |
| Platform | REST API, web dashboard, batch/queue workers | **missing** | deliberately out of scope; interfaces kept clean |
| Docs | per-feature stability labels | **partial** | README has a "what this does not do" section |

---

## 4. Proposed architecture changes

Minimal, incremental, and each one keeps every existing tool name and CLI flag.

### 4.1 `jobs.py` — a local job store (Phase 3)

```
<workdir>/jobs/<stem>-<pathhash>/
  manifest.json      state machine + timestamps + stage + error + qc summary
```

* `JobStore` with `create`, `get`, `list`, `update`, `cancel`, `cleanup`.
* States: `queued → validating → processing → quality_check → completed | failed | cancelled`.
* Written atomically; the workspace lock already serialises concurrent writers.
* No Redis, no database, no daemon: a job is a directory with a manifest, and the
  process that runs it updates the manifest at each step (already happens via `mark()`).
* Enables: `dubforge jobs list`, `dubforge_get_job_status`, cancellation (a cancel
  request file checked between steps), and honest progress for long runs.

### 4.2 `capabilities.py` — one place that answers "what can this install do"

Returns stages, providers with `verified`/`ready`, supported languages per stage, output
formats, and limits. Backs both `dubforge_get_capabilities` and the README table so they
cannot drift.

### 4.3 Provider fallback (Phase 4)

`registry.resolve_chain(kind)` returns an ordered list; `pipeline` retries the *stage*
with the next provider on `ProviderError` when `settings.fallback` is enabled. Must be
opt-in and logged: silent provider switching changes quality and cost, which is exactly
the kind of surprise this project refuses elsewhere.

### 4.4 Limits in `Settings` (Phase 3)

`max_input_seconds`, `max_input_bytes`, `max_run_seconds`, `max_usd` — checked in `plan()`
and enforced in `dub()`. Turns "it will take hours" into a refusal with a number.

### 4.5 Subtitle subsystem (Phase 2, optional)

Split `srt.py` into `subtitles/` with `read_srt`, `read_vtt`, `write_*`, `check_readability`
(CPS, line length), `translation_memory.json`. Only worth doing when a second consumer
exists; today `srt.py` is 130 lines and honest.

**Rejected on purpose:** a plugin entry-point system (the registry is a dict and works),
a database (directories are enough), an async pipeline (ffmpeg is the bottleneck, not the
event loop), and a REST server (that is a deployment decision, not a library one).

---

## 5. Prioritized roadmap

Sizes are the honest kind: **S** ≈ a day, **M** ≈ a week, **L** ≈ more than a week.

| Phase | Work | Size | Why it is next |
|---|---|---|---|
| **1 — Foundation** | content-hash cache, workspaces, locks, structured errors, CI gate, config validation, strict booleans, Windows CI | **done** (0.1.1, 0.1.2) | correctness and cross-platform reliability first |
| **2 — Dubbing intelligence** | per-word target alignment, measured CPS per language, per-segment regeneration, subtitle readability checks | M–L | the biggest remaining jump in *quality* |
| **3 — Jobs and MCP** | `jobs.py` + status/result/cancel/list, progress, cancellation, `capabilities`, `validate_config`, `cleanup_jobs`, run limits | **done** (0.2.0) | the biggest gap in *usability*, now closed |
| **4 — Provider ecosystem** | health checks, opt-in fallback, retry budgets, `providers_used` | **done** (0.2.0); live-API verification still open | needs live keys to verify honestly |
| **5 — Distribution** | PyPI trusted publishing, Docker multi-arch, batch helper in the CLI, per-platform docs | S–M | mostly mechanical |
| **6 — Platform** | REST design note, dashboard sketch, distributed worker interface | S (design only) | interfaces only, no implementation |

Recommended order: **2 → 4 (verification) → 5**. Phases 1, 3 and the code half of 4
shipped in 0.1.1–0.2.0. What is left is the quality ceiling (phase 2), live verification
of the adapters (phase 4, needs keys), and packaging (phase 5).

---

## 6. Dependency and licensing assessment

| Dependency | Licence | Kind | Note |
|---|---|---|---|
| Python 3.10+ | PSF-2.0 | required | — |
| `requests` | Apache-2.0 | required | permissive, fine |
| `imageio-ffmpeg` | BSD-2-Clause | required | ships a static **ffmpeg** binary |
| `ffmpeg` (system) | **LGPL-2.1+ or GPL-2.0+** | required at runtime | the build decides. A GPL build (e.g. with `libx264` from some distros) makes *distribution* of a bundle containing it a GPL question — relevant only if you ship a Docker image or binary bundle. Running it locally is unaffected. |
| `fastmcp` | Apache-2.0 | extra `[mcp]` | — |
| `faster-whisper` | MIT | extra `[local]` | weights: Whisper, MIT |
| `demucs` | MIT | extra `[sep]` | weights: htdemucs, MIT |
| `pyannote.audio` | MIT | extra `[diarize]` | some models need a Hugging Face token and have their own terms |
| `edge-tts` | **GPL-3.0** | extra `[free]` (installed separately today) | an unofficial client for a Microsoft endpoint. Optional and never imported unless selected. If you ship a product, this is the licence to look at. |
| Vendors (OpenAI, Deepgram, ElevenLabs, Sarvam, Google, DeepL, Groq, Sync.so) | commercial terms | runtime, user-supplied keys | not distributed with this package; their terms govern use, including voice cloning and acceptable use |

**Conclusion:** the package itself is MIT and its required dependencies are permissive.
The two things a distributor must check are the ffmpeg build's licence and the optional
`edge-tts` (GPL-3.0). Both are documented in `CREDITS.md`; neither affects a local user.

---

## 7. Security risk assessment

| Risk | Current state | Gap |
|---|---|---|
| API key leakage | keys read from the environment only, never flags, logs, reports, manifests or errors; `doctor` prints set/unset | none known; covered by tests |
| Path traversal / overwrite | every write goes through `JobWorkspace.file()`; cleanup re-resolves and refuses escapes; `output == source` refused | none known |
| Arbitrary local file read via MCP | the agent can pass any path the *user* can read — by design | **documented, not restricted**: run the server as a user with the access you are willing to grant |
| Subprocess injection | argument lists only; no `shell=True`; no `os.system` | none known; enforced by tests |
| Resource exhaustion | no limits on input size or duration | **gap** — `Settings.max_input_*` proposed (§4.4) |
| Malicious media | ffmpeg parses untrusted input; that is ffmpeg's own attack surface | **inherent**: keep ffmpeg patched, run in a sandbox for untrusted uploads |
| Prompt injection in a transcript | translator output is parsed as strict JSON, anything else discarded | mitigated, not eliminated: treat a dub of untrusted material as untrusted output |
| SSRF (if URL ingestion is added) | **not implemented** | **blocker for that feature**: require https, resolve DNS, reject private/loopback/link-local, re-validate on redirect, no DNS rebinding window |
| Voice cloning / impersonation | consent documented; no impersonation features; no identity deception | policy, not code — cannot be enforced by software |
| Data retention | nothing leaves the machine except provider calls; `--no-keep-work` cleans intermediates | **partial**: no automatic retention policy for the cache |

---

## 8. What this assessment deliberately does not do

It does not implement phases 2–6. The rules for this project say: complete, test and
review one phase before starting the next, and never advertise an unverified feature as
production-ready. Phase 1 is done and green on three operating systems; phase 3 is the
recommended next change, and it is bounded enough to review in one go.
