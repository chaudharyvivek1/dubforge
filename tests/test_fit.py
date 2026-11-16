"""The timing model: the arithmetic that decides whether a dub sounds dubbed."""

from dubforge import fit
from dubforge.media import Segment


def test_cps_lookup_falls_back_and_handles_regions():
    assert fit.cps_for("hi") == fit.CPS["hi"]
    assert fit.cps_for("hi-IN") == fit.CPS["hi"]
    assert fit.cps_for("klingon") == fit.DEFAULT_CPS


def test_estimate_scales_with_length_and_language():
    short = fit.estimate_seconds("namaste", "hi")
    long = fit.estimate_seconds("namaste " * 10, "hi")
    assert 0 < short < long
    # Devanagari-ish density: Hindi chars per second is lower than English
    assert fit.cps_for("hi") < fit.cps_for("en")


def test_fit_verdicts():
    # 2 seconds of window, a line that clearly fits
    ok = fit.fit("Yes.", 2.0, "en")
    assert ok.verdict == "ok"
    # a line that needs a light speed-up
    stretch = fit.fit("This sentence is a little too long for two seconds.", 1.9, "en")
    assert stretch.verdict in {"stretch", "rewrite"}
    # a line that cannot be squeezed without sounding wrong
    rewrite = fit.fit("x" * 200, 1.0, "en")
    assert rewrite.verdict == "rewrite"
    assert rewrite.target_chars < 200


def test_target_chars_matches_window_times_rate():
    chars = fit.target_chars(2.0, "en", 1.15)
    assert chars == int(2.0 * fit.cps_for("en") * 1.15)


def test_short_lines_are_not_flagged():
    """A line shorter than its window gets a pause, not a stretch."""
    assert fit.fit("Hi.", 4.0, "en").verdict == "ok"


def test_plan_and_needs_rewrite_use_the_window():
    segments = [
        Segment(index=0, start=0.0, end=1.0, text="short", window=1.0),
        Segment(index=1, start=1.0, end=2.0, text="y" * 300, window=1.0),
    ]
    verdicts = fit.plan(segments, "en")
    assert verdicts[0].verdict == "ok"
    assert verdicts[1].verdict == "rewrite"
    assert [s.index for s in fit.needs_rewrite(segments, "en")] == [1]


def test_constraint_note_names_the_budget():
    seg = Segment(index=3, start=0.0, end=1.0, text="z" * 100, window=1.2)
    note = fit.constraint_note(seg, "en", 1.15)
    assert "1.2s" in note
    assert "characters" in note
    assert "Rewrite it shorter" in note
