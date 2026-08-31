#!/usr/bin/env python3
"""Generate categorized GitHub release notes from Conventional Commits."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RELEASE_TAG_PATTERN = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)\Z")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
CONVENTIONAL_SUBJECT_PATTERN = re.compile(
    r"(?P<type>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?:\s+(?P<description>.+)\Z"
)
BREAKING_FOOTER_PATTERN = re.compile(r"^BREAKING(?: |-)CHANGE:\s*", re.MULTILINE)

CATEGORY_ORDER = (
    "Breaking changes",
    "Features",
    "Fixes",
    "Performance",
    "Refactoring",
    "Internationalization",
    "Documentation",
    "Tests",
    "Build and CI",
    "Maintenance",
    "Other changes",
)
TYPE_CATEGORIES = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "i18n": "Internationalization",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build and CI",
    "ci": "Build and CI",
    "chore": "Maintenance",
}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


def run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise ValueError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def release_version(tag: str) -> tuple[int, int, int]:
    match = RELEASE_TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError(f"release tag must look like 'v1.2.3', got {tag!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def find_previous_release_tag(root: Path, tag: str) -> str | None:
    """Return the closest earlier semantic-version tag reachable from ``tag``."""
    run_git(root, "rev-parse", "--verify", f"{tag}^{{commit}}")
    candidates: list[tuple[int, tuple[int, int, int], str]] = []

    for candidate in run_git(root, "tag", "--merged", tag, "--list", "v*").splitlines():
        if candidate == tag or RELEASE_TAG_PATTERN.fullmatch(candidate) is None:
            continue
        distance = int(run_git(root, "rev-list", "--count", f"{candidate}..{tag}").strip())
        candidates.append((distance, release_version(candidate), candidate))

    if not candidates:
        return None

    shortest_distance = min(candidate[0] for candidate in candidates)
    nearest = [candidate for candidate in candidates if candidate[0] == shortest_distance]
    return max(nearest, key=lambda candidate: candidate[1])[2]


def read_commits(root: Path, revision_range: str) -> list[Commit]:
    output = run_git(
        root,
        "log",
        "--reverse",
        "--no-merges",
        "--format=%H%x1f%s%x1f%b%x1e",
        revision_range,
    )
    commits: list[Commit] = []
    for raw_record in output.split("\x1e"):
        record = raw_record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f", 2)
        if len(fields) != 3:
            raise ValueError("git returned an unexpected release-history record")
        commits.append(Commit(sha=fields[0], subject=fields[1], body=fields[2].rstrip()))
    return commits


def escape_markdown(text: str) -> str:
    escaped: list[str] = []
    for character in text:
        if character in r"\`*_<>{}[]|":
            escaped.append("\\")
        escaped.append(character)
    return "".join(escaped)


def categorize_commit(commit: Commit) -> tuple[str, str]:
    match = CONVENTIONAL_SUBJECT_PATTERN.fullmatch(commit.subject)
    if match is None:
        return "Other changes", escape_markdown(commit.subject)

    commit_type = match.group("type").lower()
    is_breaking = bool(match.group("breaking")) or bool(
        BREAKING_FOOTER_PATTERN.search(commit.body)
    )
    category = "Breaking changes" if is_breaking else TYPE_CATEGORIES.get(
        commit_type, "Other changes"
    )

    description = escape_markdown(match.group("description"))
    scope = match.group("scope")
    if scope:
        description = f"**{escape_markdown(scope)}:** {description}"
    return category, description


def generate_release_notes(
    root: Path,
    tag: str,
    repository: str,
    *,
    server_url: str = "https://github.com",
) -> str:
    release_version(tag)
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"repository must look like 'owner/name', got {repository!r}")

    previous_tag = find_previous_release_tag(root, tag)
    revision_range = f"{previous_tag}..{tag}" if previous_tag else tag
    commits = read_commits(root, revision_range)
    grouped: dict[str, list[str]] = {category: [] for category in CATEGORY_ORDER}
    repository_url = f"{server_url.rstrip('/')}/{repository}"

    for commit in commits:
        category, description = categorize_commit(commit)
        commit_url = f"{repository_url}/commit/{commit.sha}"
        grouped[category].append(f"- {description} ([`{commit.sha[:7]}`]({commit_url}))")

    sections = ["## What's changed"]
    for category in CATEGORY_ORDER:
        entries = grouped[category]
        if entries:
            sections.append(f"### {category}\n\n" + "\n".join(entries))

    if not commits:
        sections.append("No changes were recorded since the previous release.")

    if previous_tag:
        changelog_label = f"{previous_tag}...{tag}"
        changelog_url = f"{repository_url}/compare/{previous_tag}...{tag}"
    else:
        changelog_label = f"Commit history through {tag}"
        changelog_url = f"{repository_url}/commits/{tag}"
    sections.append(f"**Full changelog:** [{changelog_label}]({changelog_url})")

    return "\n\n".join(sections) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag to describe, for example v1.2.3")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="GitHub repository as owner/name (defaults to GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        help="GitHub server URL (defaults to GITHUB_SERVER_URL)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write Markdown to this file instead of standard output",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (defaults to this script's parent directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.repository:
        print("Release notes: FAIL: --repository or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 1

    try:
        notes = generate_release_notes(
            args.root,
            args.tag,
            args.repository,
            server_url=args.server_url,
        )
        if args.output:
            args.output.write_text(notes, encoding="utf-8")
        else:
            print(notes, end="")
    except (OSError, ValueError) as error:
        print(f"Release notes: FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
