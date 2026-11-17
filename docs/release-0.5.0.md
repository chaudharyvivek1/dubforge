# 0.5.0 release checklist

Partial remixing: a regeneration splices the changed lines into the previous dialogue track
instead of rebuilding it. Patch-level behaviour change, minor-version bump because the reuse
contract is now public (it is reported in `review.json` and in `capabilities`).

## 1. Why this milestone

0.4.0 reused renders and the separated bed, then rebuilt the dialogue track from scratch: one
`adelay`/`amix` chain per clip, in passes of 48. A 300-line film therefore paid for the whole
track on every regeneration, whatever the number of edits.

## 2. What shipped

* `dialogue.py`: the assembled track plus a per-line manifest (start, rendered duration,
  speed, clip SHA-256) and a SHA-256 of the track.
* Reuse ladder, in order: **reused** (no line changed) → **spliced** (some changed, none
  overlapping) → **assembled** (everything else).
* The splice mutes each changed line's own extent and mixes the new render in at the same
  start, in one pass: `volume=enable='between(t,S,E)':volume=0` per line plus one `amix`.
* A changed line that overlaps another refuses the splice — zeroing a shared range would
  delete the other speaker.
* `review.json` reports `work.dialogue_mode` and `work.spliced_lines`, and
  `work.rebuilt_stages` no longer lists the dialogue track when it was not rebuilt.

## 3. Correctness argument (and its test)

The spliced track must equal a from-scratch rebuild. `test_a_splice_equals_a_full_rebuild`
assembles three clips, replaces the middle one, splices, then assembles the same three clips
from scratch and compares the two files sample by sample: the mean absolute difference must
stay under 1% of full scale. `test_only_the_changed_range_is_touched` checks the neighbouring
lines survive in the regions they occupy.

## 4. Compatibility

* No public name changed. One setting (`dialogue_cache`, default true).
* A workspace with no `dialogue.json` assembles as before; a corrupt or truncated track is a
  miss, not an error.
* `DubResult` gained `dialogue_mode` and `spliced_lines`; `review.json` gained the matching
  `work` keys.

## 5. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       579 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed
python scripts/benchmark.py --sizes small measured
python scripts/benchmark.py --regen       full run vs 0/1/5/10% edits, with the dialogue mode
python -m build + twine check             PASSED
```

## 6. Benchmark (small clip, stub providers)

| case | TTS | separations | reused renders | bed | dialogue track | wall |
|---|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | assembled | 6.3 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | reused | 4.6 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | spliced | 5.1 s |
| regen, 3 edited | 3 | 0 | 0 | 1 | assembled | 5.1 s |

With three lines, "all edited" leaves nothing to reuse, so the ladder correctly falls back to
assembly. The saving shows up on long films, where the track is many passes and the edits are
few.

## 7. Remaining limitations

* Overlapping dialogue always costs a full assembly.
* Normalisation, ducking, mix and mux still run on every regeneration.
* Word-level alignment remains provider-dependent and unverified live.
* Calibration has no measured samples.

## 8. Recommended next milestone

Cache the mix stage for the untouched head and tail of the film (the ducking envelope is
recomputable from the dialogue), and add a per-stage timing report to `review.json` so a user
can see where a regeneration spends its time.
