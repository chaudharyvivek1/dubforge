# 0.6.0 release checklist

Stage-level reuse and honest timing: a regeneration reuses every stage whose inputs are
unchanged, and the run report says which stages actually ran.

## 1. Why this milestone

0.5.0 spliced the dialogue track, so a one-line edit stopped paying for the assembly. But
every run still normalised, ducked, mixed and muxed: three audio passes and an encode, even
when **nothing** had changed. A no-op regeneration of a long film cost minutes to reproduce a
file it already had.

## 2. The ladder, with its stopping point

| stage | key | a hit skips |
|---|---|---|
| TTS render | text, voice, provider, model, language, style, emotion, sample rate | the paid call |
| bed | source hash, separator, model, keep-background, sample rate | separation |
| dialogue track | per-line clip hashes + starts, track SHA-256 | the assembly passes |
| mix | dialogue hash, bed hash, gains, duck, loudness target, true peak, runtime, rate | loudnorm, ducking, mix |
| mux | mix hash, source hash, bitrate, has-video | the audio encode |

**The stopping point is the interesting part.** The mix is *not* reused when the dialogue was
spliced. Loudness normalisation applies one gain to the whole track; a changed line moves that
gain, and every untouched region moves with it. Splicing the mix would hand back a file the
settings did not ask for. So a splice misses the mix key by construction — the dialogue's hash
is in it — and the run reports `mix_mode: "rebuilt"`.

`test_a_spliced_dialogue_always_rebuilds_the_mix` exists so that a future "optimisation" of
this has to argue with a test rather than with a comment.

## 3. Correctness evidence

* `test_an_unchanged_run_reuses_every_stage` monkeypatches `loudnorm`, `duck_and_mix`, `mux`
  and `assemble_dub` to record their calls, runs the same job twice, and asserts the second
  run calls **none** of them — then compares the two output files byte for byte.
* `test_the_mix_key_covers_every_input_that_changes_the_mix` varies each input in turn,
  including that two files with the same bytes under different names are the *same* key
  (content, not path).
* `test_a_truncated_stage_artifact_is_a_miss` and `test_a_stage_without_metadata_is_a_miss`
  keep the cache honest about corruption.

## 4. Compatibility

* No public name changed. One setting (`stage_cache`, default true).
* Caches live under `<workdir>/cache/{mix,mux}`; a missing or corrupt entry is a miss, never an
  error, and `dubforge cache info|clean` still applies to the stem cache.
* `DubResult` gained `mix_mode` and `mux_mode`; `review.json` gained the matching `work` keys
  plus the timing summary.

## 5. Verification (actually run)

```
ruff check src tests scripts              clean
ruff format --check src tests scripts     clean
python -m pytest -q                       593 tests
python scripts/gen_reference.py --check   current
python -m dubforge doctor                 ok
python scripts/ci_demo_check.py           passed
python scripts/benchmark.py --sizes small measured
python scripts/benchmark.py --regen       full run vs 0/1/5/10% edits, with mix/mux columns
python -m build + twine check             PASSED
```

## 6. Benchmark (small clip, stub providers)

| case | TTS | separations | reused renders | bed | dialogue | mix | mux | wall |
|---|---|---|---|---|---|---|---|---|
| full run | 3 | 1 | 0 | 0 | assembled | rebuilt | rebuilt | 6.6 s |
| regen, 0 edited | 0 | 0 | 3 | 1 | reused | reused | reused | 2.9 s |
| regen, 1 edited | 1 | 0 | 2 | 1 | spliced | rebuilt | rebuilt | 5.2 s |

The 2.3× on the no-op case is the whole point: on a 9-second clip the fixed costs dominate.
On a long film the reused stages are all O(length), so the ratio grows with the film.

Note the honest oddity in the printed table: a "3 edited" row can show `mix reused`, because
with stub providers two different texts can render byte-identical audio. Reuse is
content-addressed, and the benchmark prints a line saying so.

## 7. Remaining limitations

* A spliced dialogue always rebuilds the mix — per-region loudness gains would be needed to do
  better, and this loudness model does not have them.
* Overlapping dialogue still costs a full assembly.
* Word-level alignment is provider-dependent and unverified live; calibration has no measured
  samples.

## 8. Recommended next milestone

Per-region loudness: measure and apply a gain envelope so a splice can reuse the mix outside
the changed windows. After that, a `dubforge explain <job>` command that reads `review.json`
and prints the cost story in one screen.
