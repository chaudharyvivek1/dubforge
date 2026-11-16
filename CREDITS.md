# Credits

dubforge is a pipeline, not an engine. Almost everything that actually sounds good
was built by someone else, and this file is where that is written down — including the
licence each piece comes under, because a dubbing tool that gets a licence wrong is a
liability to whoever uses it.

## Standing on

| Project | What dubforge uses it for | Licence |
|---|---|---|
| [FFmpeg](https://ffmpeg.org/) | every decode, filter, mix, mux and measurement in the pipeline | LGPL-2.1+ / GPL-2.0+ depending on build |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | ships a static ffmpeg so a machine with none still works | BSD-2-Clause |
| [Python](https://python.org) | the language | PSF-2.0 |
| [requests](https://requests.readthedocs.io/) | HTTP for every network adapter | Apache-2.0 |
| [FastMCP](https://github.com/jlowin/fastmcp) | the MCP server layer (optional extra) | Apache-2.0 |
| [Model Context Protocol](https://modelcontextprotocol.io/) | the tool contract an agent sees | MIT |

## Speech recognition

| Project / service | Notes | Licence / terms |
|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (SYSTRAN) | CTranslate2 implementation of Whisper, used by the local adapter | MIT |
| [Whisper](https://github.com/openai/whisper) (OpenAI) | the model family behind the local and hosted adapters | MIT (model weights MIT) |
| [Deepgram](https://deepgram.com/) | Nova-3 with diarisation | commercial API |
| [ElevenLabs](https://elevenlabs.io/) | Scribe v2 | commercial API |
| [Groq](https://groq.com/) | Whisper large-v3-turbo inference | commercial API |
| [OpenAI](https://platform.openai.com/) | Whisper API | commercial API |

## Translation

| Service | Notes | Terms |
|---|---|---|
| [OpenAI](https://platform.openai.com/) | chat models, the default translator | commercial API |
| [Google Gemini](https://ai.google.dev/) | long-context translation | commercial API |
| [Sarvam AI](https://sarvam.ai/) | Mayura, built for Indian languages | commercial API |
| [DeepL](https://www.deepl.com/) | European pairs | commercial API |
| [Ollama](https://ollama.com/) | local model runner | MIT |

## Voice

| Service / project | Notes | Terms |
|---|---|---|
| [ElevenLabs](https://elevenlabs.io/) | multilingual v2, the reference for voice identity | commercial API |
| [Sarvam AI](https://sarvam.ai/) | Bulbul v2, Indic voices | commercial API |
| [OpenAI](https://platform.openai.com/) | gpt-4o-mini-tts / tts-1 | commercial API |
| [Google Cloud TTS](https://cloud.google.com/text-to-speech) | Neural2 and Studio voices | commercial API |
| [edge-tts](https://github.com/rany2/edge-tts) | free Microsoft neural voices, unofficial endpoint | GPL-3.0 (client); the endpoint's terms are Microsoft's |

## Separation and lip sync

| Project / service | Notes | Licence / terms |
|---|---|---|
| [Demucs](https://github.com/adefossez/demucs) (Alexandre Défossez) | htdemucs two-stem separation | MIT |
| [PyTorch](https://pytorch.org/) | runtime for Demucs and faster-whisper | BSD-3-Clause |
| [Sync.so](https://sync.so/) | Sync-2 lip sync | commercial API |

## Ideas and prior art

* The timing model — window per line, characters-per-second budget, rewrite before
  stretch — is the standard working method of audiovisual translation, applied to
  machine translation. The arithmetic is dubforge's; the craft is the dubbing
  industry's.
* The shape of this repository (one MCP server, one agent skill, a `doctor` command, a
  `verified` flag on adapters) follows [forge3d](https://github.com/chaudharyvivek1/forge3d),
  this project's sibling.
* The prompt in `providers/translate.py` borrows its structure from dubbing style
  guides: say what it means, fit the time, stay in character.

## Price data

List prices in `src/dubforge/pricing.py` and `docs/cost.md` were read from the
vendors' own pricing pages in November 2026. They are quoted as estimates with their
source, not as a quote, and they change.

## Not affiliated

dubforge is an independent project. It is not affiliated with, endorsed by, or
supported by any of the vendors above, and each vendor's own terms govern your use of
their API — including their rules on voice cloning, consent and acceptable use.
