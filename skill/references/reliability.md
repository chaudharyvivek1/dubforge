# Reliability: retries, idempotency, health, fallback, events

Everything here exists because a dub costs money and takes hours. The failure modes worth
knowing are not "it crashed" — they are "it charged twice" and "it said done when it was not".

## Retries: what is safe, and what is not

Two kinds of failure, and they are not the same:

| Failure | Retried? | Why |
|---|---|---|
| `429 rate_limit` (an answer) | yes, honouring `Retry-After` | the vendor rejected the request; nothing was billed |
| `5xx server` (an answer) | yes, bounded backoff | same |
| timeout / connection dropped, **non-idempotent call** | **no** | the vendor may have accepted and billed it |
| timeout, `idempotent=True` or a vendor-documented idempotency key | yes | a duplicate is harmless |
| `401` / `403` `auth` | no | retrying cannot fix a key |
| `400` / `422` `bad_request` | no | retrying cannot fix the request |

The third row is the one that costs money. It raises
`provider_error` with `kind: "ambiguous"` and a message saying the request may have been
accepted and billed. **Do not retry it automatically.** Say so to the user, then either
check the vendor dashboard or re-run that single line deliberately.

Only OpenAI's `Idempotency-Key` is used, because it is the one this project's adapters can
point at a vendor document for. No header is invented for any other provider, and no call is
marked idempotent on a guess. The retry budget is `max_retries` (default 2), and every retry
is counted and reported:

* `provider_retries` in the job document;
* a warning in the run ("N provider retries … no request was retried after an ambiguous
  timeout");
* a `Provider retries` section in `report.md`;
* a `provider.retry` event in the event log.

## Provider health

```bash
dubforge providers                     # readiness, capability, quality note, verified flag
```

`health(live=True)` (MCP: `dubforge_check_provider_health {"live": true}`) makes one cheap
authenticated call where the vendor has a free endpoint — OpenAI, ElevenLabs, Groq,
Deepgram, DeepL, Ollama, edge-tts. Every other adapter says "no cheap probe" rather than
pretending. `live=False` never touches the network.

A passing live check proves the **credential works**, not that the model's output is good.
`verified` stays `false` until someone runs a real request and records the date and API
version — that is a manual, deliberate act, and a test enforces that it is not flipped by
accident.

## Fallback

`--fallback` / `DUBFORGE_FALLBACK=1` walks the provider chain when a stage fails. It is
**off by default**, because a different provider means different quality and different cost.

When it fires: the run's warnings say which provider failed and which one took over
("falling back to X"), voices are re-mapped if the voice provider changed, and a transcript
produced by a fallback recogniser is not cached under the requested one's key.
`providers_used` records what actually ran, separately from what was asked for.

## Events

```bash
DUBFORGE_EVENTS=/tmp/dubforge.jsonl dubforge batch lessons/ --to hi
tail -f /tmp/dubforge.jsonl | jq -r '.event + " " + (.job // "")'
```

One JSON object per line, off unless you ask. Stable event names: `job.created`,
`job.claimed`, `job.stage`, `job.completed`, `job.failed`, `job.cancelled`, `job.retry`,
`job.recovered`, `provider.retry`, `provider.fallback`, `qc.result`, `cleanup`.

Every record carries `at` (epoch seconds) plus identifiers — job id, stage, provider,
attempt, error kind. No key, no request header, no transcript text ever appears, and a log
that cannot be written never breaks the work it is logging.

## What to check when a run looks wrong

| Symptom | Look at |
|---|---|
| "it finished but the file is bad" | `qc.failures` — `completed` is not `qc_ok` |
| "it took twice as long as expected" | `provider_retries`, and the `job.stage` events |
| "it charged more than the estimate" | `providers_used` (did fallback switch to a dearer provider?), `provider_retries` |
| "it says interrupted but I never stopped it" | was the process killed? `error_kind: interrupted` plus `attempt_history` |
| "it said done and there is no file" | impossible by design: `completed` requires an output path that exists — if you see it, that is a bug worth reporting |
