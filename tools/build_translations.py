#!/usr/bin/env python3
"""Compile every PO file into the standard gettext locale layout."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DOMAIN = "io.github.leesethefox.Clipper"
REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "build" / "locale")
    args = parser.parse_args()
    for po_file in sorted((REPO_ROOT / "po").glob("*.po")):
        subprocess.run(
            ["msgcmp", str(po_file), str(REPO_ROOT / "po" / "clipper.pot")],
            check=True,
        )
        for attribute in ("--untranslated", "--only-fuzzy"):
            result = subprocess.run(
                ["msgattrib", attribute, "--no-obsolete", "-o", "-", str(po_file)],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                state = "untranslated" if attribute == "--untranslated" else "fuzzy"
                print(f"{po_file}: contains {state} messages", file=sys.stderr)
                return 1
        destination = args.output_dir / po_file.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["msgfmt", "--check", "--check-format", "-o", str(destination), str(po_file)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
