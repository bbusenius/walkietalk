#!/usr/bin/env bash
# Agent-solo TX prove: real keying via `walkietalk play WAV --transmit`.
# Usage: solo-tx.sh [duration_seconds]
# Preconditions: doctor hardware=ready; authorized --transmit; no other holder of PTT serial.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
dur="${1:-1.2}"
wav="/tmp/walkietalk-verify-solo-tx.wav"

if ! command -v walkietalk >/dev/null 2>&1; then
  echo "solo-tx: walkietalk not on PATH" >&2
  exit 127
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "solo-tx: ffmpeg required to build the tone fixture" >&2
  exit 127
fi

ffmpeg -y -f lavfi -i "sine=frequency=800:duration=${dur}" -ar 16000 -ac 1 "$wav" >/dev/null 2>&1
exec "$ROOT/helpers/run.sh" talk-transmit -- walkietalk play "$wav" --transmit
