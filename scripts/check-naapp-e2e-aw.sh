#!/usr/bin/env bash
# E2E must exercise the aw release this repository pins, whatever else is installed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
expected="aw $("$ROOT/scripts/install-aw.sh" --version)"
selected="$(command -v aw || true)"
reported="$([[ -n "$selected" ]] && AW_NO_UPDATE_CHECK=1 aw version 2>/dev/null | head -n 1 || true)"
if [[ "$reported" != "$expected" ]]; then
  echo "naapp e2e requires $expected on PATH (make aw); found ${reported:-no aw}${selected:+ at $selected}" >&2
  exit 1
fi
echo "$reported at $selected"
