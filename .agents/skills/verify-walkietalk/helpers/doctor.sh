#!/usr/bin/env bash
# Read-only readiness for walkietalk verification. Never transmits.
set -euo pipefail
if ! command -v walkietalk >/dev/null 2>&1; then
  echo "doctor: walkietalk not on PATH" >&2
  exit 127
fi

echo "=== walkietalk --version ==="
walkietalk --version

echo "=== config-check ==="
walkietalk config-check
cc=$?

echo "=== devices ==="
walkietalk devices || true

echo "=== check (never transmits) ==="
set +e
walkietalk check
chk=$?
set -e

hw=missing
if walkietalk devices 2>/dev/null | grep -qiE 'AllInOneCable|AIOC'; then
  if [[ "$chk" -eq 0 ]]; then
    hw=ready
  else
    hw=partial
  fi
fi

echo "=== doctor verdict ==="
echo "config-check_exit=$cc check_exit=$chk hardware=$hw"
echo "Transmit features require hardware=ready. Prefer config-check when hardware=missing."
exit 0
