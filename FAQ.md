# FAQ

Real questions, answered without marketing.

### Do I need API keys to try it?

No. `dubforge demo` runs the entire pipeline offline — real ffmpeg, real mixing, real
QC — and produces an mp4. The "voice" is a tone and the "translator" passes text
through, and QC fails the run on purpose so you cannot mistake it for a finished dub.

### Can I dub a video with no keys at all, for real?

Yes, with three packages: `faster_whisper` (local recognition), `ollama` (local
translation), `edge_tts` (free Microsoft voices). Quality is decent rather than
great, and it is slow on a laptop, but nothing leaves the machine and nothing is
billed. See [docs/school.md](docs/school.md).

### Which provider combination is actually best?

```
asr        deepgram     (diarisation + word timings)
translator openai       (obeys a character budget, so the rewrite loop works)
tts        sarvam       (Indian languages) or elevenlabs (voice identity)
separator  demucs
lipsync    none
```

About $0.75 for a 10-minute video at list prices. The cheapest respectable version is
`groq` + `openai` + `edge_tts` at about $0.03.

### Why does the dub sound rushed?

The translator did not shorten enough and atempo hit its cap. Check
`max_speed_applied` in QC. Fixes, in order of effectiveness: use an LLM translator,
raise `--rewrite-rounds` to 3, lower `--max-speed` to 1.08 so more lines go back for
rewrite.

### Why is everyone in one voice?

The recogniser did not diarise. `--asr deepgram` or `--asr elevenlabs`. If you know who
speaks when, `--voice-map SPEAKER_00=<voice>,SPEAKER_01=<voice>`.

### The music is gone.

No bed was produced — `background_kept` in QC will say so. Install `demucs` (`pip
install demucs`), or check the source is stereo for `ffmpeg_center`. Mono sources
cannot be separated by the mid/side fallback.

### Can it dub into Hindi, Tamil, Bengali…?

Yes — those are the reason it exists. Voice quality depends on the provider: Sarvam
Bulbul is the best without cloning, `edge_tts` has real Indic neural voices for free,
ElevenLabs clones a voice across languages. Read [docs/languages.md](docs/languages.md)
for the traps (script, numbers, formality, gender agreement).

### Can it clone a specific person's voice?

Technically yes (ElevenLabs), and only with that person's written permission. A voice
clone is a deepfake with a good excuse; publish with a clear "dubbed" label, and keep
the consent with the project. [docs/security.md](docs/security.md) has the detail.

### Does it change the picture?

No. The video stream is copied, never re-encoded. Lip sync (`--lipsync synclabs`) is
the only thing that touches frames, it is off by default, and it needs public URLs.

### Will it fit a 2-hour film?

It will run; whether it *fits* is a translation problem, not a tool problem. `dubforge
plan film.mp4 --to hi` tells you the runtime, the character estimate and the bill
before you commit. Expect heavy rewriting on dense dialogue — that is normal, and it is
what the rewrite rounds are for.

### How do I fix a few bad lines without re-paying for everything?

Edit `transcript.dub.json`, then re-run with `--transcript`. Recognition and translation
are skipped; only synthesis runs. `examples/library_use.py` shows the same thing in
code.

### Is the transcript cached?

Yes, keyed by **SHA-256 of the file's contents** plus the ASR provider, model, source
language and diarisation flag. Re-running with a different translator or voice does not
re-pay for recognition; moving or renaming the file does not either; two different files
that share a name, a size and a timestamp never collide. `--no-cache` ignores it.

### Where do the working files go?

`<workdir>/jobs/<stem>-<hash>/`, one directory per source path, with a lock so two dubs
of the same file cannot collide. An interrupted run resumes in the same directory —
`manifest.json` says which steps finished. Transcripts live in
`<workdir>/cache/<content-key>/` and are shared between jobs.

### Why does QC say "FAIL: translated" on the demo?

Because the demo uses a stub translator that returns text unchanged. That is the
feature: a pipeline test can never look like a finished dub. Configure a real translator
and the check passes.

### What does "UNVERIFIED" mean on a provider?

The adapter is written against the vendor's documentation and has not been exercised
against the live API in this release. Treat the first output as a test, not a delivery.
Only the offline adapters and the ffmpeg paths are verified today.

### Why does it refuse to run instead of falling back to something free?

Because a dub that silently did nothing is worse than an error that names the missing
key. `auto` picks the best *ready* provider; if none is ready it says exactly what is
missing for each candidate.

### Can I use it commercially?

The code is MIT. The voices, models and APIs are not: each vendor's terms govern your
use, including whether a cloned voice may be used commercially. Read them.

### Does anything leave my machine?

Only what a provider needs: the audio to the recogniser, the text to the translator, one
line at a time to the voice. No telemetry, no dubforge server, no proxy. The offline
stack sends nothing at all.

### Can I submit a long job and check on it later?

Yes. `dubforge_submit_job` (or `dubforge batch` from the CLI) queues the work and returns
a job id; `dubforge_get_job_status` reports stage and progress, `dubforge_get_job_result`
collects the paths and QC. Jobs survive a restart: a job whose process died comes back as
`interrupted` — never as completed — and can be retried.

### Can I cancel a run?

`dubforge_cancel_job` / `dubforge jobs cancel <id>`. It is cooperative: the pipeline checks
between stages and before each paid call, so the current line finishes and an ffmpeg run
in progress is not killed. That is stated in the output rather than promised away.

### What are the limits, and can I change them?

Seven, all configurable in the environment or a settings file: input size (4 GiB), input
duration (4 h), wall-clock run time (4 h), estimated cost ($25), concurrent jobs (2),
retries per provider call (2), job retention (30 days). They refuse rather than warn, and
the error names the variable to change — `DUBFORGE_MAX_ESTIMATED_USD=5` for a tight lab
budget.

### Is there an API server?

No, deliberately. `dubforge.dub()` is a library — wrap it in FastAPI or whatever your
school needs. A dubbing service that accepts uploads is an upload service, and its
security model is yours to define.

### Does it work on Windows?

CI runs the full suite, the demo gate and a workspace-locking check on
`windows-latest` on every push, so "yes" is a tested claim rather than a hope. There are
three known annoyances: `PATH` after a `winget` install, 260-character paths in deep
work directories, and console encoding for Indic scripts. All three are in
[docs/platforms.md](docs/platforms.md).

### How long does a dub take?

Recognition and translation are minutes for a long file; synthesis is the bottleneck and
it is network-bound (or CPU-bound locally). Roughly: 10-minute video, API stack, 3–8
minutes; fully local stack, 20–40 minutes on a laptop CPU.

### Why "forge" again?

The sibling project is [forge3d](https://github.com/chaudharyvivek1/forge3d): one MCP
server, one agent skill, a `doctor`, and adapters that admit when they are unverified.
Same shape, different craft.

### Can I help?

Yes. The most useful contribution right now is verifying one network adapter against a
live key and flipping its `verified` flag. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[ROADMAP.md](ROADMAP.md).
