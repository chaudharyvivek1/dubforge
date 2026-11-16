#!/usr/bin/env bash
# Dub one video, print the QC verdict, and show where everything landed.
#
#   export DEEPGRAM_API_KEY=… OPENAI_API_KEY=… SARVAM_API_KEY=…
#   ./examples/dub_one.sh lesson.mp4 hi
#
# Exit code: 0 = QC passed, 1 = QC failed (read the report), 2 = could not run.
set -euo pipefail

INPUT="${1:?usage: dub_one.sh <video> <target-lang> [extra dubforge flags]}"
TARGET="${2:?usage: dub_one.sh <video> <target-lang> [extra dubforge flags]}"
shift 2

dubforge doctor >/dev/null || { echo "run 'dubforge doctor' — this machine is not ready"; exit 2; }

dubforge dub "$INPUT" --to "$TARGET" \
  --asr deepgram --translator openai --tts sarvam \
  --style "warm, unhurried teacher" \
  --rewrite-rounds 2 \
  "$@"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  echo "QC passed. Proofread the bilingual SRT before publishing."
else
  echo "QC failed — read the report above. Nothing here is ready to show anyone."
fi
exit "$STATUS"
