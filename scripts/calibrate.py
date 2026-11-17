#!/usr/bin/env python3
"""Calibrate speech rate from measurements, and report what is still estimated.

    python scripts/calibrate.py --report                 # print the report
    python scripts/calibrate.py --report --write         # + data/calibration/measured_cps.json
                                                         # + docs/calibration.md
    python scripts/calibrate.py --measure --provider edge_tts --languages hi,ta --yes-live

`--measure` renders a fixed sentence set through a real provider, which costs money or
bandwidth, so it refuses to run without `--yes-live`. Nothing in the test suite or CI calls
it. Samples carry their provenance (provider, voice, model, method, date) because a number
without provenance is not a measurement.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dubforge import calibration  # noqa: E402
from dubforge.config import Settings  # noqa: E402
from dubforge.providers import registry  # noqa: E402

# A fixed set, one per language, chosen to be representative rather than convenient: a
# declarative sentence, a question, a list and a number, which is where speech rate moves.
SENTENCES = {
    "hi": [
        "आज सुबह स्कूल में सभी छात्र एक साथ इकट्ठे हुए।",
        "क्या आपने नई किताब पढ़ ली है?",
        "पहला, दूसरा, तीसरा और चौथा अध्याय।",
        "साल दो हज़ार छब्बीस में यह परीक्षा होगी।",
    ],
    "gu": [
        "આજે સવારે શાળામાં બધા વિદ્યાર્થીઓ ભેગા થયા.",
        "તમે નવું પુસ્તક વાંચ્યું છે?",
        "પહેલો, બીજો, ત્રીજો અને ચોથો પ્રકરણ.",
        "વર્ષ બે હજાર છવ્વીસમાં આ પરીક્ષા થશે.",
    ],
    "ta": [
        "இன்று காலை பள்ளியில் அனைத்து மாணவர்களும் கூடினர்.",
        "புதிய புத்தகத்தைப் படித்தீர்களா?",
        "முதல், இரண்டாம், மூன்றாம், நான்காம் பாடம்.",
        "ஆயிரத்து இருபத்து ஆறாம் ஆண்டில் இந்தத் தேர்வு நடைபெறும்.",
    ],
    "te": [
        "ఈ ఉదయం పాఠశాలలో విద్యార్థులందరూ కలిసారు.",
        "మీరు కొత్త పుస్తకం చదివారా?",
        "మొదటి, రెండవ, మూడవ, నాల్గవ అధ్యాయం.",
        "రెండు వేల ఇరవై ఆరు సంవత్సరంలో ఈ పరీక్ష జరుగుతుంది.",
    ],
    "bn": [
        "আজ সকালে স্কুলে সব শিক্ষার্থী একসাথে জড়ো হলো।",
        "আপনি কি নতুন বইটি পড়েছেন?",
        "প্রথম, দ্বিতীয়, তৃতীয় ও চতুর্থ অধ্যায়।",
        "দুই হাজার ছাব্বিশ সালে এই পরীক্ষা হবে।",
    ],
    "mr": [
        "आज सकाळी शाळेत सर्व विद्यार्थी एकत्र जमले.",
        "तुम्ही नवीन पुस्तक वाचले आहे का?",
        "पहिला, दुसरा, तिसरा आणि चौथा धडा.",
        "दोन हजार सव्वीस मध्ये ही परीक्षा होईल.",
    ],
    "en": [
        "All the students gathered in the school this morning.",
        "Have you read the new book yet?",
        "The first, second, third and fourth chapters.",
        "This examination will take place in the year two thousand twenty six.",
    ],
}


def count_chars(text: str) -> int:
    """The counting convention this project uses, in one place."""
    return len("".join((text or "").split()))


def measure(provider_name: str, languages: list[str], settings: Settings, tmp: Path) -> list:
    provider = registry.create("tts", provider_name, settings)
    provider.require()
    tmp.mkdir(parents=True, exist_ok=True)
    made = []
    for language in languages:
        sentences = SENTENCES.get(language)
        if not sentences:
            print(f"  {language}: no fixed sentence set, skipped", file=sys.stderr)
            continue
        voices = provider.voices(language)
        voice = voices[0].id if voices else ""
        for index, text in enumerate(sentences):
            out = str(tmp / f"{language}-{index}.wav")
            rendered = provider.synthesize(text, voice, out)
            duration = float(rendered.duration or 0.0)
            if duration <= 0.05:
                print(f"  {language}/{index}: provider returned no audio, skipped", file=sys.stderr)
                continue
            made.append(
                calibration.Sample(
                    language=language,
                    chars=count_chars(text),
                    duration_s=round(duration, 3),
                    counting=calibration.COUNTING,
                    method="tts-render",
                    voice=voice or "(provider default)",
                    provider=provider_name,
                    script=language,
                    model=getattr(settings, "tts_model", "") or "",
                    sample_rate=48000,
                    style="neutral",
                    measured_at=time.strftime("%Y-%m-%d"),
                )
            )
            print(
                f"  {language}/{index}: {count_chars(text)} chars in {duration:.2f}s "
                f"({count_chars(text) / duration:.1f} cps)"
            )
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate speech rate from measurements.")
    parser.add_argument("--report", action="store_true", help="print the calibration report")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write data/calibration/measured_cps.json and docs/calibration.md",
    )
    parser.add_argument("--measure", action="store_true", help="render fixed sentences (live)")
    parser.add_argument("--provider", default="", help="TTS provider to measure with")
    parser.add_argument("--languages", default="hi,gu,ta,te,bn,mr,en")
    parser.add_argument(
        "--yes-live",
        action="store_true",
        help="required for --measure: it spends money or bandwidth",
    )
    parser.add_argument(
        "--samples", default="", help="dataset path (default data/calibration/samples.jsonl)"
    )
    parser.add_argument("--min-samples", type=int, default=calibration.MIN_SAMPLES)
    parser.add_argument("--tmp", default="/tmp/dubforge-calibrate")
    args = parser.parse_args(argv)

    dataset = Path(args.samples) if args.samples else None

    if args.measure:
        if not args.yes_live:
            print(
                "refusing to make live provider requests without --yes-live "
                "(it costs money or bandwidth)",
                file=sys.stderr,
            )
            return 2
        if not args.provider:
            print("--measure needs --provider", file=sys.stderr)
            return 2
        languages = [code.strip() for code in args.languages.split(",") if code.strip()]
        settings = Settings()
        made = measure(args.provider, languages, settings, Path(args.tmp))
        if made:
            calibration.append_samples(made, dataset)
            print(f"appended {len(made)} measured sample(s)")
        else:
            print("no samples appended", file=sys.stderr)

    samples, problems = calibration.load(dataset)
    text = calibration.report(samples, problems)
    if args.report or not (args.measure or args.write):
        print(text)
    if problems:
        print(f"{len(problems)} row(s) rejected — see the report", file=sys.stderr)

    if args.write:
        overrides = calibration.calibrated(samples, args.min_samples)
        target = calibration.write_measured(overrides)
        (ROOT / "docs" / "calibration.md").write_text(text, encoding="utf-8")
        print(f"wrote {target.relative_to(ROOT)} ({len(overrides)} language(s) calibrated)")
        print("wrote docs/calibration.md")
        if not overrides:
            print(
                "no language reached the sample threshold, so fit.CPS estimates stay in use "
                "and are reported as estimates",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
