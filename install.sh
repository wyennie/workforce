#!/usr/bin/env bash
set -euo pipefail

REPO="${WORKFORCE_REPO:-git+https://github.com/wyennie/workforce}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing from https://astral.sh/uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv's installer puts the binary in ~/.local/bin (or $XDG_BIN_HOME); make it usable for the rest of this script
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
fi

uv tool install --force "$REPO"

if ! command -v workforce >/dev/null 2>&1; then
  echo
  echo "workforce installed, but not on PATH yet."
  echo "Run: uv tool update-shell    (then restart your shell)"
  exit 0
fi

workforce doctor
