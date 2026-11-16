# Running it in a school

This is the deployment this project was built for: a media lab or a language
department with a few laptops, a set of lesson videos, and no budget for per-minute
dubbing APIs. Everything below runs on one ordinary machine.

## The zero-cost, offline setup

```bash
pip install "dubforge[local,sep,mcp] @ git+https://github.com/chaudharyvivek1/dubforge.git"
pip install edge-tts
# one-time, ~2 GB of model weights, needs a machine with 8 GB RAM
ollama pull qwen2.5:14b-instruct
```

```bash
dubforge dub lesson.mp4 --to hi \
  --asr faster_whisper --asr-model small \
  --translator ollama --translate-model qwen2.5:14b-instruct \
  --tts edge_tts --voice-pool hi-IN-SwaraNeural \
  --separator ffmpeg_center \
  --context "Class 9 biology lesson on photosynthesis"
```

Nothing leaves the machine: recognition is local, translation is local, separation is
local. `edge_tts` is the one network call, and it needs no key. Total cost: zero.

Expect roughly real-time-to-3× real-time on a laptop CPU for a 10-minute video. Run a
class set overnight rather than in class.

## The paid setup, when quality matters

Same command with three keys and better voices — this is what to use for anything
shown to parents or published:

```bash
export DEEPGRAM_API_KEY=…        # diarisation, best timings
export OPENAI_API_KEY=…          # translation with the timing constraint
export ELEVENLABS_API_KEY=…      # the voice

dubforge dub lesson.mp4 --to ta \
  --asr deepgram --translator openai --tts elevenlabs \
  --voice-pool <teacher-voice-id> \
  --style "warm, unhurried teacher" \
  --context "Class 9 biology lesson on photosynthesis"
```

About ₹65 for a 10-minute lesson at list prices (see [cost.md](cost.md)).

## A term's workflow

1. **Consent first, always.** A teacher's voice and likeness, and any student on
   camera, need written permission before a dub is published. Keep a signed sheet per
   video. See [security.md](security.md#consent).
2. **Dub, then gate on QC.** Anything with `qc.ok == false` does not go to students.
3. **Proofread the bilingual SRT.** Two minutes with `lesson.bilingual.srt` catches
   the terminology errors no check can.
4. **Watch three minutes** — first, middle, last.
5. **Publish** the mp4 plus the target SRT (captions matter for accessibility, and
   they are free once the dub exists).

```bash
dubforge dub lessons/*.mp4 --to hi     # no: one file per run
for f in lessons/*.mp4; do dubforge dub "$f" --to hi --json > "${f%.mp4}.qc.json"; done
```

There is a batch helper in [`examples/classroom_batch.py`](../examples/classroom_batch.py)
that runs a folder, keeps a CSV of every file's QC verdict, and refuses to overwrite a
previous good output.

## Storage and housekeeping

* One 10-minute dub leaves about 150 MB of intermediates. `--workdir` onto an external
  drive, or `--no-keep-work` after a good run.
* Keep `transcript.dub.json` even when you delete the rest: it is what lets you
  re-voice a lesson next term without paying for recognition again
  (`--transcript`).
* The ASR cache is keyed by file fingerprint, so moving the source file invalidates
  it. Copy the work directory with the video if you archive.

## Roles on a shared machine

`dubforge` has no accounts and no server, which is a feature in a lab. If several
people share a machine:

* give each person their own `--workdir`;
* put keys in that person's environment, never in a file in the repo;
* `dubforge doctor` reports whether a key is set, never its value, so a screenshot of
  it is safe to paste into a ticket.

## If you need a shared service

Run the MCP server behind your own supervisor, or wrap `dubforge.dub()` in a small
FastAPI app — the pipeline is a library first:

```python
from dubforge import Settings, dub

result = dub("lesson.mp4", Settings(target_lang="hi", tts="sarvam"))
if not result.qc["ok"]:
    raise SystemExit(f"QC failed: {result.qc['failures']}")
```

There is no built-in HTTP API on purpose: a dubbing service that accepts uploads is a
file-upload service with all the security questions that implies, and those answers
are specific to your school.

## A realistic first week

| Day | What |
|---|---|
| 1 | `dubforge demo`, then `dubforge doctor` on the lab machine. Confirm ffmpeg filters and that a demo mp4 plays. |
| 2 | One 2-minute clip, `edge_tts` free voices, read the bilingual SRT. Judge the *translation*, not the voice. |
| 3 | Same clip with a real voice (Sarvam for Indic, ElevenLabs for cloning). Compare. |
| 4 | A whole lesson, `--separator demucs`, listen for the bed surviving. |
| 5 | Batch the term's lessons overnight with `examples/classroom_batch.py`. |
