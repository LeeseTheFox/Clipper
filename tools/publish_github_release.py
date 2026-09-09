#!/usr/bin/env python3
"""Stage complete release assets before publication; never replace a public release."""

import json
import os
import re
import subprocess


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def version_tuple(tag):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError(f"Unexpected stable tag: {tag}")
    return tuple(map(int, tag[1:].split(".")))


def main():
    tag, version, bundle = os.environ["TAG"], os.environ["VERSION"], os.environ["BUNDLE"]
    current = version_tuple(tag)
    if tag != f"v{version}":
        raise ValueError("Version and tag differ")
    # Only a confirmed HTTP 404 means no release exists. Authentication and
    # network failures must not be mistaken for an absent release.
    repository = os.environ["GITHUB_REPOSITORY"]
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repository}/releases"))
    releases = [release for page in releases for release in page]
    existing = next((release for release in releases if release["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        raise RuntimeError("This release is already public; refusing to overwrite its assets")
    newer = any(
        not release["draft"]
        and not release["prerelease"]
        and version_tuple(release["tag_name"]) > current
        for release in releases
    )
    if not existing:
        gh(
            "release",
            "create",
            tag,
            "--draft",
            "--verify-tag",
            "--title",
            f"Clipper {version}",
            "--notes-file",
            "release-notes.md",
        )
    gh(
        "release",
        "upload",
        tag,
        bundle,
        "Clipper-x86_64.flatpak",
        "update-x86_64.json",
        "SHA256SUMS",
        "--clobber",
    )
    gh(
        "release",
        "edit",
        tag,
        "--draft=false",
        "--title",
        f"Clipper {version}",
        "--notes-file",
        "release-notes.md",
        f"--latest={'false' if newer else 'true'}",
    )


if __name__ == "__main__":
    main()
