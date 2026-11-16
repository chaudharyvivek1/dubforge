# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

Pre-1.0, only the latest release gets fixes.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/chaudharyvivek1/dubforge/security/advisories/new)
rather than a public issue. If that is unavailable, email the address on
[@chaudharyvivek1](https://github.com/chaudharyvivek1)'s profile.

Please include: what you did, what happened, the version (`dubforge --version`), your
OS, and — if it is a pipeline bug — the `qc.json` from the run.

Expect an acknowledgement within a few days. This is a single-maintainer project, so
please do not publish details before there is a fix or 90 days have passed, whichever
comes first.

## What counts as a security issue here

* A way for a **provider response**, a **transcript** or a **media file** to make
  dubforge leak a key, write outside `--workdir`, or execute something.
* A key appearing in a log, a report, an error message, `qc.json`, a transcript or a
  subprocess command line.
* A path traversal in output naming (a media file whose name escapes the work
  directory).
* Anything that makes an untrusted input reach a shell, a filter graph or a template
  without quoting.

## What does not

* A dub that sounds bad. That is a quality bug — open a normal issue with the QC report.
* An unverified adapter failing against a live API. That is expected; see
  [docs/providers.md](docs/providers.md).
* A vendor changing their API or price. Same.
* The cost of a run. `dubforge plan` exists so this is never a surprise, but it is not
  a security boundary.

## The design decisions that matter

* **Keys live in the environment only.** They are never accepted as CLI flags (where
  they would land in shell history and `ps`), never written to disk, never logged.
  `dubforge doctor` reports whether a variable is set, never its value.
* **No uploads on your behalf.** The lip sync adapter requires public URLs and
  explicitly refuses local paths rather than quietly uploading your footage somewhere.
* **No server, no telemetry, no proxy.** There is nothing of dubforge's between you and
  the vendor.
* **Untrusted text is data.** A translator's response is parsed as JSON and anything
  that is not a line of translation is discarded; a transcript never becomes a command.

Consent, data retention and voice cloning are covered at length in
[docs/security.md](docs/security.md) — read that before dubbing a real person.
