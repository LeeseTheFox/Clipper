#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
venv_python="$repo_root/venv/bin/python"

if [[ ! -x "$venv_python" ]]; then
    printf 'error: expected the local Python environment at ./venv/bin/python\n' >&2
    exit 2
fi

cd "$repo_root"
exec "$venv_python" "$script_dir/benchmark_flatpak_startup.py" "$@"
