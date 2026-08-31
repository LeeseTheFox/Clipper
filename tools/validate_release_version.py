#!/usr/bin/env python3
"""Verify that a release tag matches every Clipper application version."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def version_from_tag(tag: str) -> str:
    if not tag.startswith("v"):
        raise ValueError(f"release tag must start with 'v', got {tag!r}")

    version = tag[1:]
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            f"release tag must be an annotated semantic version such as 'v1.2.3', got {tag!r}"
        )
    return version


def read_version_values(root: Path) -> dict[str, str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    pyproject_match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    if pyproject_match is None:
        raise ValueError("could not find [project] version in pyproject.toml")

    main_py = (root / "ui" / "main.py").read_text(encoding="utf-8")
    about_match = re.search(r'^\s*version="([^"]+)",$', main_py, re.MULTILINE)
    if about_match is None:
        raise ValueError("could not find Adw.AboutWindow version in ui/main.py")

    engine = (root / "engine" / "src" / "clipper_engine.c").read_text(encoding="utf-8")
    engine_match = re.search(r'^#define ENGINE_VERSION\s+"([^"]+)"$', engine, re.MULTILINE)
    if engine_match is None:
        raise ValueError("could not find ENGINE_VERSION in engine/src/clipper_engine.c")

    metainfo = ET.parse(root / "packaging" / "flatpak" / "io.github.leesethefox.Clipper.metainfo.xml")
    release = metainfo.find("./releases/release")
    if release is None or "version" not in release.attrib:
        raise ValueError("could not find the current AppStream release version")

    return {
        "pyproject.toml": pyproject_match.group(1),
        "ui/main.py About dialog": about_match.group(1),
        "engine/src/clipper_engine.c": engine_match.group(1),
        "Flatpak AppStream release": release.attrib["version"],
    }


def validate_release_version(root: Path, tag: str) -> str:
    version = version_from_tag(tag)
    values = read_version_values(root)
    mismatches = [f"{location} is {value!r}" for location, value in values.items() if value != version]
    if mismatches:
        raise ValueError(
            f"release tag {tag!r} requires application version {version!r}, but "
            + "; ".join(mismatches)
        )
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag to verify, for example v1.2.3")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (defaults to this script's parent directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        version = validate_release_version(args.root, args.tag)
    except (OSError, ET.ParseError, ValueError) as error:
        print(f"Release version check: FAIL: {error}", file=sys.stderr)
        return 1

    print(f"Release version check: PASS ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
