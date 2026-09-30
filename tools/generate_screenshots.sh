#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

if ! command -v flatpak >/dev/null 2>&1; then
    echo "error: screenshot generation requires Flatpak" >&2
    exit 1
fi
if [[ ! -x ./venv/bin/python ]]; then
    python3 -m venv ./venv
fi
exec ./venv/bin/python tools/generate_screenshots.py "$@"
