# 0.7.0 release checklist

The last two passes, and a command that explains the bill. A regeneration that changes nothing
now reuses every stage.

## 1. Why this milestone, and why not the one that was proposed

0.6.0's note said: *reuse the mix for the untouched head and tail of a film, with per-region
loudness.* Before building it, I measured a full run per stage:

| stage | seconds |
|---|---|
| extract 16 kHz mono + 48 kHz stereo | 0.33 |
| separate stems | 0.21 |
| synthesise | 1.03 |
| assemble dialogue track | 0.21 |
| normalise loudness + mix | 1.09 |
| mux | 0.75 |

Per-region loudness would have to run a loudness measurement pass and a splice pass to replace
two passes, and it moves a global gain into a per-region one — a correctness problem in
exchange for at most one pass.

Two other stages were free to fix and scale with the film:

* **extraction** — two full decodes of the source on every run, and a regeneration needs the
  same two files;
* **stretch** — one ffmpeg process per line, so 300 processes on a 300-line regeneration.

So the milestone became: cache what scales with the film, and ship the command that shows a
user where their time went. The decision is written up in `docs/architecture.md` under "What a
stage costs" so the next person does not have to re-derive it.

## 2. What shipped

* `mix.extract_key()` + stage cache for the decoded audio, keyed on source content hash, rate
  and channels. `extract_mode` reports `reused` / `partial` / `rebuilt`.
* `<seg_id>.placed.wav` in the segment store: the render after its window-specific atempo,
  keyed on the speed applied, checksummed, with its measured duration stored so reuse costs no
  `ffprobe` call. `placed_reused` counts them.
* `explain.py` + `dubforge explain` + `dubforge_explain_job` (26 tools): which stages ran,
  where the seconds went, QC failures, and what one more edit would cost.
* `work.rebuilt_stages` gained `extraction`, and `review.json` gained `extract_mode` and
  `placed_reused`.

## 3. Correctness evidence

* `test_a_second_run_does_not_decode_the_source_again` monkeypatches `extract_audio`, runs the
  same job twice, and asserts the second run calls it **zero** times.
* `test_the_stretched_render_is_reused_when_the_window_is_unchanged` does the same for
  `stretch`, and asserts `placed_reused >= 1`.
* `test_a_changed_window_invalidates_the_placed_render` and `test_a_placed_record_without_a_checksum_is_a_miss`
  keep the cache honest: a different speed or a damaged artifact is a miss.
* `test_explain_reads_the_review_rather_than_guessing` and the no-op variant assert the story
  matches the report, including `rebuilt_stages == []`.

## 4. Compatibility

* No public name changed. `DubResult` gained `extract_mode` and `placed_reused`; `review.json`
  gained the matching `work` keys.
* Caches live under `<workdir>/cache/audio` and inside each job's `segments/`; a missing or
  corrupt entry is a miss. A 0.6.0 workspace keeps working and warms these caches on its next
  run.
* `dubforge explain` is additive; it reads the same `review.json` that 0.4.0 introduced.

## 5. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       608 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed
python scripts/benchmark.py --sizes small measured
python scripts/benchmark.py --regen       full run vs 0/1/5/10% edits
python -m build + twine check             PASSED
```

## 6. Benchmark (small clip, stub providers)

| case | TTS | separations | reused renders | bed | dialogue | mix | mux | wall |
|---|---|---|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | assembled | rebuilt | rebuilt | 6.2 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | reused | reused | reused | 2.3 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | spliced | rebuilt | rebuilt | 4.3 s |

Across the series: a no-op regeneration went 6.6 s (0.5.0) → 2.9 s (0.6.0) → 2.3 s (0.7.0),
and the remaining wall clock is process startup and bookkeeping rather than media work.

## 7. Remaining limitations

* A spliced dialogue still rebuilds the mix. The measured analysis says the trade is right at
  this stage size; a per-region gain model would change that, and is the honest next step only
  if the mix ever dominates a real film's regeneration.
* Word-level alignment is provider-dependent and unverified live; calibration has no measured
  samples.
* A no-op run is cheap, not free: ~2 s of startup on the demo.

## 8. Recommended next milestone

Live verification. Everything that remains on the honesty ledger is the same item: no adapter
has been exercised against a real API. `scripts/verify_providers.py` is ready; the milestone is
to run it with real keys, record the results in `docs/verification.json`, flip `verified` only
where a row says `ok`, and then use that data to fill the calibration dataset with measured
speech rates. It is the one thing that cannot be done from here, and it is worth more than
another cache.
