#!/usr/bin/env bash
# Thin source-checkout entrypoint. Configuration and provisioning belong to Python.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if ! command -v mise >/dev/null 2>&1; then
  echo 'Install mise 2026.10.17 or newer: https://mise.jdx.dev/installing-mise.html' >&2
  exit 1
fi
export MEMORY_WORLDS_SETUP_CWD=$PWD
cd "$root"
# mise enforces its normal trust check. Never trust or modify shell profiles here.
mise install --locked python uv
exec mise run setup -- "$@"
