#!/bin/bash
# Quick launch script for Clipper UI

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="python3"
if [ -x "$SCRIPT_DIR/../venv/bin/python" ] && \
   "$SCRIPT_DIR/../venv/bin/python" -c "import gi" >/dev/null 2>&1; then
    PYTHON="$SCRIPT_DIR/../venv/bin/python"
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" main.py "$@"
