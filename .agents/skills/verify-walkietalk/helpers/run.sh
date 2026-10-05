#!/usr/bin/env bash
# Usage: run.sh <feature-id> -- <command...>
# Saves cmd/stdout/stderr/exit under artifacts/<feature-id>/<run-id>/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -lt 3 || "$2" != "--" ]]; then
  echo "usage: $0 <feature-id> -- <command...>" >&2
  exit 2
fi
feature="$1"
shift 2
run_id="$(date +%Y%m%dT%H%M%S)-$$"
outdir="$ROOT/artifacts/$feature/$run_id"
mkdir -p "$outdir"
printf '%s\n' "$*" >"$outdir/cmd.txt"
set +e
"$@" >"$outdir/stdout.txt" 2>"$outdir/stderr.txt"
ec=$?
set -e
printf '%s\n' "$ec" >"$outdir/exit.txt"
echo "evidence: $outdir (exit $ec)"
exit "$ec"
