# Repository setup checklist

Things GitHub cannot take from the files in this repo. Copy-paste values, in order,
when publishing a fork or re-publishing this project.

## About → Description

```
Dub any video into any language with several AI providers in one quality-controlled pipeline: diarised transcription, duration-aware translation, per-speaker voices, music kept, ten QC checks. MCP server + agent skill.
```

## About → Topics

Paste these one at a time (GitHub's topic box accepts them comma-separated on some
screens, one per line on others):

```
dubbing
video-dubbing
audiovisual-translation
translation
text-to-speech
tts
speech-recognition
asr
diarization
voice-cloning
lip-sync
ffmpeg
mcp
model-context-protocol
ai-agents
agent-skill
indic-languages
hindi
tamil
python
```

## About → Website

Leave empty, or point it at your published docs.

## Social preview

Settings → General → Social preview → upload `docs/demo/banner.png` (1280×400). GitHub
recommends 1280×640; the banner is padded by GitHub on the card and reads fine.

## Features

* Issues: **on** — the forms in `.github/ISSUE_TEMPLATE/` ask for the QC report.
* Discussions: **on** if you want a place for "why does my dub sound like this".
* Wiki: **off** — the `docs/` folder is the documentation, and a wiki drifts from it.
* Projects: **off**.

## Branch protection for `main`

* Require the `test` check from `ci.yml` to pass before merging.
* Require a pull request for changes (solo maintainers can still push hotfixes; the
  point is that CI must see them).
* Do not require signed commits unless you already sign.

## PyPI (only when you actually want `pip install dubforge`)

`.github/workflows/release.yml` builds, `twine check`s and publishes on a `v*` tag
through trusted publishing — no token in the repo. Before the first release:

1. PyPI → Publishing → add a pending publisher: repository `dubforge`, workflow
   `release.yml`, environment `pypi`.
2. GitHub → Settings → Environments → new environment `pypi` (no secrets needed).

Until that is done the workflow builds and checks but cannot publish, and the install
line in the README is `pip install "dubforge @ git+https://github.com/…"`.

## README badges

The `ci` badge points at `ci.yml` on `main`. The `tests` badge is a static
`56%20passing` shield — keep it honest by updating it when the count changes, or delete
it and let the CI badge speak.
