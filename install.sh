#!/usr/bin/env bash
set -euo pipefail

REPO_BASE="https://github.com/wyennie/workforce"
TAG=""
LIST_TAGS=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      TAG="$2"
      shift 2
      ;;
    --list-tags)
      LIST_TAGS=true
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: install.sh [--tag vX.Y.Z] [--list-tags]" >&2
      exit 1
      ;;
  esac
done

# --list-tags: show available releases and exit
if $LIST_TAGS; then
  echo "Available Workforce releases:"
  git ls-remote --tags "$REPO_BASE" \
    | awk '{print $2}' \
    | grep -E 'refs/tags/v[0-9]' \
    | grep -v '\^{}' \
    | sed 's|refs/tags/||' \
    | sort -V
  exit 0
fi

# Ensure uv is available
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing from https://astral.sh/uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv's installer puts the binary in ~/.local/bin (or $XDG_BIN_HOME)
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
fi

# Resolve install source: tagged ref, env-var override, or HEAD
if [[ -n "$TAG" ]]; then
  INSTALL_SOURCE="git+${REPO_BASE}@${TAG}"
else
  INSTALL_SOURCE="${WORKFORCE_REPO:-git+${REPO_BASE}}"
fi

uv tool install --force "$INSTALL_SOURCE"

if ! command -v workforce >/dev/null 2>&1; then
  echo
  echo "workforce installed, but not on PATH yet."
  echo "Run: uv tool update-shell    (then restart your shell)"
  exit 0
fi

# Print the installed version
INSTALLED_VERSION="$(uv tool run --from workforce workforce --version 2>/dev/null \
  || python3 -c "import importlib.metadata; print(importlib.metadata.version('workforce'))" 2>/dev/null \
  || echo "unknown")"

echo
echo "Installed workforce ${INSTALLED_VERSION}."
workforce doctor
