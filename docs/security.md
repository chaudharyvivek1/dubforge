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

## Reporting a problem

If you find a way for a provider response, a transcript or a media file to make
dubforge do something it should not — leak a key, write outside the work directory,
execute something — open an issue with the reproduction and the QC report. Those are
treated as security bugs, not feature requests.
