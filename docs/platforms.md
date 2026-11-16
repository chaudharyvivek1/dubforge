# Platforms

dubforge is pure Python plus one binary (ffmpeg). There is no compiler step, no GPU
requirement and no server. What varies between machines is where ffmpeg comes from and
which optional engines are worth installing.

## ffmpeg resolution

In this order, and `dubforge doctor` prints which one won:

1. `$DUBFORGE_FFMPEG` — an explicit path.
2. `ffmpeg` on `PATH`.
3. The static binary inside `imageio-ffmpeg`, installed as a hard dependency.

So a machine with no system ffmpeg still runs, and a machine with a good system build
still uses it. dubforge checks for the specific filters it needs (`atempo`, `adelay`,
`amix`, `apad`, `atrim`, `loudnorm`, `aresample`, `sidechaincompress`,
`silencedetect`, `volumedetect`, `aformat`) and says exactly which are missing rather
than failing halfway through a run. Very old or stripped builds (some NAS packages,
some minimal Docker images) are the usual culprits.

| OS | Recommended install |
|---|---|
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` |
| Any, no admin rights | `pip install imageio-ffmpeg` (already a dependency) |
| Docker | see the `Dockerfile` — `python:3.12-slim` + `apt install ffmpeg` |

## Linux

The reference platform and what CI runs. Everything works, including `demucs` on CPU
and `faster_whisper` with `int8` quantisation.

## macOS

Works on Intel and Apple Silicon. `brew install ffmpeg` is the sane path.
`faster_whisper` uses the CPU; `demucs` will use MPS if torch supports it, otherwise
CPU. Watch out for `PATH`: an app launched from Finder does not see your shell's
`PATH`, so set `DUBFORGE_FFMPEG` in the launch environment if you drive it from a
shortcut.

## Windows

Works, with three specific annoyances:

* **PATH.** `winget install Gyan.FFmpeg` does not always add itself to `PATH` for an
  already-open terminal. Reopen it, or `set DUBFORGE_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe`.
* **Long paths.** Deep work directories plus long file names can exceed the 260
  character limit. Use a short `--workdir` like `C:\dub`.
* **Console encoding.** The CLI prints Devanagari, Tamil and Bengali to the console.
  On a legacy code page that raises `UnicodeEncodeError`; use Windows Terminal, or
  `chcp 65001`, or `--json` and read the file instead.

## Docker

```bash
docker build -t dubforge .
docker run --rm -v "$PWD:/data" -e OPENAI_API_KEY dubforge \
  dub /data/lesson.mp4 --to hi
```

The image carries ffmpeg and the package. Mount a work directory so the outputs
survive the container.

## CPU-only agent sandboxes

The demo and the whole stub path run with no network and no keys, which is deliberate:
it means an agent can prove the pipeline works before it is given credentials. For
real work in a sandbox, remember that `faster_whisper` and `demucs` download model
weights on first use, so the sandbox needs outbound HTTPS once (or a pre-warmed cache
volume).

## Hardware guidance

| Task | Minimum | Comfortable |
|---|---|---|
| Demo, stub pipeline | any CPU | any |
| `faster_whisper` `small`, 10 min video | 2 cores, 4 GB RAM | 4 cores, 8 GB |
| `faster_whisper` `large-v3` | 8 GB RAM | GPU with 6 GB VRAM |
| `demucs` `htdemucs`, 10 min | 4 cores, 8 GB | GPU |
| Everything else | network-bound | — |

`--asr-model tiny` and `--asr-model base` exist for very small machines; accuracy on
Indic languages drops sharply below `small`, so test before batching.

## Determinism and interruption

Every stage writes to the work directory as it finishes, and the transcript is cached
by fingerprint. A run that dies at the synthesis stage can be resumed cheaply: the
transcription is not paid for twice. `--dry-run` resolves providers and prints the
estimate without touching a paid API, which is what to run in CI.
