# 0.2.0 release checklist

What is done, what was verified and how, and what a releaser still has to do by hand.
Nothing here is aspirational: each line is either ticked with evidence or explicitly open.

## 1. Scope

| Area | State |
|---|---|
| Job engine (`jobs.py`): documents, lifecycle, recovery, runner | **done** |
| Background execution, progress, cooperative cancellation | **done** |
| MCP tools: 10 → 21 (job control + intelligence) | **done** |
| Capabilities, config validation, cost estimation | **done** |
| Resource and cost limits | **done** |
| Provider health, failure classification, `Retry-After`, opt-in fallback, `providers_used` | **done** |
| Batch processing with failure isolation and resume | **done** |
| CLI: `jobs`, `capabilities`, `validate-config`, `batch` | **done** |
| Segment-level regeneration, target alignment, subtitle readability | **designed, not implemented** (see `assessment.md`) |
| Multi-track audio export | **not started** (needs output probing) |
| URL ingestion | **not started, deliberately** (SSRF work first) |
| REST API / dashboard / distributed workers | **not started, deliberately** |

## 2. Backward compatibility

* All ten original MCP tool names and their schemas are unchanged.
* Every existing CLI command and flag still works; new ones are additive.
* `pipeline.dub()` gained two keyword arguments (`cancel`, `progress`) with defaults, so
  existing calls are unaffected.
* `registry.resolve()` keeps its behaviour; `resolve_chain()` is new beside it.
* `Settings` gained seven fields plus `fallback`; all have defaults and validation.
* Job documents live in a new `registry/` directory — no existing file was reshaped.

## 3. Verification

Run and recorded on Linux (3.10, 3.11, 3.12), macOS 3.12 and Windows 3.12 in CI:

- [x] `ruff check src tests scripts`
- [x] `ruff format --check src tests scripts`
- [x] `python -m pytest -q` — 309 tests
- [x] `python -m pytest -q tests/test_concurrency.py tests/test_workspace.py tests/test_server.py` (run separately)
- [x] `python -m dubforge doctor`
- [x] `python scripts/ci_demo_check.py` — the offline demo, with its expected QC failure
- [x] MCP startup, all 21 tools registered, structured error for a bad path
- [x] Windows workspace check: locking, isolation, stale-lock reclaim, cleanup
- [x] `python -m build` + `twine check` (wheel and sdist)

Not verifiable without keys, and therefore **not claimed**:

- [ ] any network adapter against a live API (`verified: false` everywhere)
- [ ] live health probes against real credentials
- [ ] real provider cost figures (list prices only, with sources)

## 4. Manual steps for a human releaser

1. **PyPI** — add the trusted publisher for `release.yml` / environment `pypi`, then push a
   `v0.2.0` tag. Until that exists, `pip install dubforge` does not resolve and the git
   install line in the README is the install path.
2. **Repository metadata** — description, topics and social preview are in
   `.github/REPO_SETUP.md`; the GitHub App used by this account cannot set them.
3. **Tag and release notes** — `git tag -a v0.2.0 -m "job engine, 21 MCP tools, limits"`,
   then paste the `CHANGELOG.md` section as the release body.
4. **Smoke test from a clean machine** — `pip install "dubforge[mcp] @ git+https://…@v0.2.0"`,
   then `dubforge demo` and `dubforge capabilities`.

## 5. Known limitations shipping in 0.2.0

* Cancellation is cooperative; in-flight provider calls and ffmpeg runs are not killed.
* Network adapters remain `verified: false`.
* `dubforge_analyze_video` was not added: `probe_media` + `plan_dub` already cover it.
* Job recovery depends on pid liveness (POSIX) or heartbeat age (Windows, where probing a
  pid is unsafe). A shared work directory across machines therefore relies on heartbeats.
* The batch interface is CLI/library only — no MCP batch tool, because `submit_job` in a
  loop is the same thing with better visibility.

## 6. Rollback

`0.2.0` adds directories (`registry/`, `out/`) and settings; it does not migrate or delete
anything. Reverting to `0.1.2` leaves those directories in place and ignored. Jobs created
by 0.2.0 are simply not visible to 0.1.2's CLI, which has no job commands.
