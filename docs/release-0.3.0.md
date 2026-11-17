# 0.3.0 release checklist

Quality release: correct timing, honest alignment, selective regeneration. No public name
changed; one new MCP tool and one new CLI command were added.

## 1. Audit findings (before any code changed)

| # | Finding | Severity | Evidence |
|---|---|---|---|
| 1 | `windows()` could give a line more room than existed before the next line | **P0** | a 0.3 s segment followed 0.35 s later received 0.4 s and collided |
| 2 | `docs/quality.md` said "fifteen checks" and listed `multiple_audio_tracks` twice | P0 | two rows, one stale count |
| 3 | `ROADMAP.md` carried stale test and QC counts | P1 | "371 tests", "21 checks" |
| 4 | The README described job directories by source path; jobs are request-addressed | P1 | `jobs/lecture-a1b2c3d4/` |
| 5 | `--transcript` was documented and ignored — the pipeline always ran ASR | **P1 (new)** | no `settings.transcript` read anywhere in `pipeline.py` |
| 6 | QC checks lost their declared severity on success, so a passing warning was indistinguishable from a passing blocker | P1 (new) | `severity` became `"pass"` |

## 2. Reproduction

```
# 1
windows([Segment(0, 0.0, 0.30), Segment(1, 0.35, 1.0)], total_duration=1.0)
  -> [0.4, ...]   # 0.4 > 0.35 − 0.0, so the dub line overlaps the next one

# 5
dubforge dub clip.mp4 --transcript supplied.json   # ASR still runs; the file is never read
```

Both are now tests: `tests/test_timeline_alignment.py::test_a_window_never_exceeds_the_room_before_the_next_line`
and `tests/test_segments.py::test_a_supplied_transcript_is_used_instead_of_recognition`.

## 3. What shipped

* timeline rules for seven edge cases, with the collision floor removed and overlap preserved;
* `alignment.py` — measured word timings or an explicit "none", never an estimate;
* `calibration.py` + `scripts/calibrate.py` + an empty, schema-documented dataset;
* `segments.py` + `pipeline.regenerate()` + `dubforge regen` + `dubforge_regenerate_segments`;
* three per-line QC checks and `review.json`;
* documentation corrected, and the docs test now compares check names rather than a number.

## 4. Compatibility

* No public name changed. New: one MCP tool (22 total), one CLI command (`regen`), six
  settings with defaults.
* `Segment` gained fields; `Transcript`/`Segment` JSON from 0.2.x still loads (unknown keys
  are ignored, missing ones default).
* A workspace with no `segments/` directory renders everything, exactly as before.
* `qc.json` gained `declared` per check and kept `severity`; existing consumers are unaffected.

## 5. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       499 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed (expected QC failure verified)
python scripts/benchmark.py --sizes small measured
python scripts/calibrate.py --report      report generated, 0 measured samples
python -m build + twine check             wheel and sdist PASSED
```

CI runs the same on Linux 3.10/3.11/3.12, macOS 3.12 and Windows 3.12.

## 6. Provider verification status

Unchanged and honest: `scripts/verify_providers.py` and `docs/verification.json` remain the
only source of truth, the record is still empty, every network adapter is `verified: false`,
and no test or CI run makes a paid request. `--measure` in the calibration script requires
`--yes-live` for the same reason.

## 7. Remaining limitations

* Sentence-level timing everywhere, because no adapter here reports word timings live.
* Calibration has no measured samples; the estimates are labelled as estimates.
* Regeneration rebuilds the whole mix (ASR skipped, lines reused) rather than remixing
  partially — the next milestone.
* QC cannot judge translation accuracy, emotional authenticity or voice similarity, and the
  documentation says so.

## 8. Recommended next milestone

Partial remixing (reuse the mixed bed and re-place only changed lines), then a review
workflow that consumes `review.json` and feeds edits back through `dubforge regen`.
