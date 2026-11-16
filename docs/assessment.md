# Assessment: architecture, features and roadmap

Written before the next phase of work, as the project's own rules require: inspect,
report, then implement in separate reviewed changes. **Nothing in the "missing" tables
below is implemented yet.** Where a feature is partial, it says exactly how far it goes.

Current release: **0.1.2**. Everything in "implemented" is tested; everything else is
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
| `qc.py` | fifteen checks, metrics, one-screen summary | `fail` exits non-zero |
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
| Jobs | job lifecycle (`queued → processing → qc → done`), job ids, status, result, cancel, list | **missing** | `manifest.json` records steps but nothing reads it as a job |
| Jobs | progress reporting, cancellation, retry of a failed stage | **missing** | a run blocks the caller |
| MCP | `dubforge_get_capabilities`, `validate_config`, `job_status`, `job_result`, `cancel_job`, `list_jobs`, `cleanup_workspace`, `check_provider_health` | **missing** | see §4 for the ones worth adding |
| MCP | `translate_transcript`, `generate_speech` as separate stage tools | **missing** | today only the whole pipeline and transcription are exposed |
| Providers | fallback chain when a provider fails mid-run | **missing** | `auto` picks once, at the start |
| Providers | health check beyond "is the key set" | **partial** | only `ollama` probes its server |
| Providers | rate-limit aware retry with backoff budget | **partial** | fixed 2 retries with 2/4/8 s backoff |
| Audio | multi-track export (dialogue / music / effects in one container) | **missing** | the mix is single-track |
| Audio | silence trimming, pitch control, per-segment regeneration | **missing** | atempo only, pitch preserved by design |
| Subtitles | line-length limits, reading-speed (CPS) validation, translation memory | **missing** | subtitle text is whatever the translation is |
| Input | URL ingestion | **missing** | deliberate: it needs SSRF protection first (§7) |
| Limits | maximum input size, maximum duration, per-run budget | **missing** | a 4-hour file will simply take hours |
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
| **3 — Jobs and MCP** | `jobs.py` + status/result/cancel/list, progress, cancellation, `capabilities`, `validate_config`, `cleanup_workspace`, run limits | M | the biggest remaining gap in *usability* |
| **4 — Provider ecosystem** | health checks, opt-in fallback, integration tests behind a flag, one or two verified adapters | M | needs live keys to verify honestly |
| **5 — Distribution** | PyPI trusted publishing, Docker multi-arch, batch helper in the CLI, per-platform docs | S–M | mostly mechanical |
| **6 — Platform** | REST design note, dashboard sketch, distributed worker interface | S (design only) | interfaces only, no implementation |

Recommended order: **3 → 2 → 4 → 5**. Phase 3 because every agent caller wants it and it
is bounded; phase 2 because it is the quality ceiling; phase 4 because it needs keys that
must be tested deliberately; phase 5 last because it is only packaging.

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
