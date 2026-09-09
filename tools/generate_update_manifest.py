#!/usr/bin/env python3
"""Describe the exact validated Flatpak bundle for Clipper's updater."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("Expected a semantic version")
    app_id = "io.github.leesethefox.Clipper"
    ref = subprocess.check_output(
        ["flatpak", "--user", "info", "--show-ref", app_id], text=True
    ).strip()
    commit = subprocess.check_output(
        ["flatpak", "--user", "info", "--show-commit", app_id], text=True
    ).strip()
    if not re.fullmatch(r"app/io\.github\.leesethefox\.Clipper/x86_64/[\w.-]+", ref):
        parser.error("Unexpected installed application reference")
    digest = hashlib.sha256()
    with args.bundle.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    manifest = {
        "schema": 1,
        "version": args.version,
        "asset": args.bundle.name,
        "ref": ref,
        "commit": commit,
        "size": args.bundle.stat().st_size,
        "sha256": digest.hexdigest(),
    }
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
