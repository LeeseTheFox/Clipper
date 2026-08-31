from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "generate_release_notes.py"
SPEC = importlib.util.spec_from_file_location("generate_release_notes", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_notes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_notes
SPEC.loader.exec_module(release_notes)


def git(root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Clipper Test",
            "GIT_AUTHOR_EMAIL": "clipper@example.com",
            "GIT_COMMITTER_NAME": "Clipper Test",
            "GIT_COMMITTER_EMAIL": "clipper@example.com",
        }
    )
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout


def commit(root: Path, subject: str, body: str = "") -> str:
    marker = root / "history.txt"
    previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
    marker.write_text(f"{previous}{subject}\n", encoding="utf-8")
    git(root, "add", "history.txt")
    arguments = ["commit", "--quiet", "-m", subject]
    if body:
        arguments.extend(["-m", body])
    git(root, *arguments)
    return git(root, "rev-parse", "HEAD").strip()


def initialize_repository(root: Path) -> None:
    git(root, "init", "--quiet")


def test_generate_release_notes_groups_commits_since_previous_tag(tmp_path):
    initialize_repository(tmp_path)
    commit(tmp_path, "chore: establish baseline")
    git(tmp_path, "tag", "-a", "v1.0.0", "-m", "Clipper 1.0.0")
    feature_sha = commit(tmp_path, "feat(editor): add timeline snapping")
    fix_sha = commit(tmp_path, "fix: preserve export settings")
    commit(tmp_path, "docs: explain Flatpak installation")
    commit(tmp_path, "feat(engine)!: replace capture protocol", "BREAKING CHANGE: new protocol")
    git(tmp_path, "tag", "-a", "v1.1.0", "-m", "Clipper 1.1.0")

    notes = release_notes.generate_release_notes(
        tmp_path, "v1.1.0", "LeeseTheFox/Clipper"
    )

    assert "### Breaking changes" in notes
    assert "**engine:** replace capture protocol" in notes
    assert "### Features" in notes
    assert "**editor:** add timeline snapping" in notes
    assert "### Fixes" in notes
    assert "preserve export settings" in notes
    assert "### Documentation" in notes
    assert "establish baseline" not in notes
    assert f"/commit/{feature_sha}" in notes
    assert f"/commit/{fix_sha}" in notes
    assert "[v1.0.0...v1.1.0]" in notes
    assert "/compare/v1.0.0...v1.1.0" in notes


def test_generate_release_notes_uses_full_history_for_first_release(tmp_path):
    initialize_repository(tmp_path)
    commit(tmp_path, "Initial implementation [preview]")
    git(tmp_path, "tag", "-a", "v1.0.0", "-m", "Clipper 1.0.0")

    notes = release_notes.generate_release_notes(
        tmp_path, "v1.0.0", "LeeseTheFox/Clipper"
    )

    assert "### Other changes" in notes
    assert r"Initial implementation \[preview\]" in notes
    assert "[Commit history through v1.0.0]" in notes
    assert "/commits/v1.0.0" in notes


def test_find_previous_release_tag_ignores_non_semantic_tags(tmp_path):
    initialize_repository(tmp_path)
    commit(tmp_path, "chore: establish baseline")
    git(tmp_path, "tag", "-a", "v1.0.0", "-m", "Clipper 1.0.0")
    commit(tmp_path, "chore: create preview")
    git(tmp_path, "tag", "-a", "v1.1.0-rc1", "-m", "Preview")
    commit(tmp_path, "fix: finish release")
    git(tmp_path, "tag", "-a", "v1.1.0", "-m", "Clipper 1.1.0")

    assert release_notes.find_previous_release_tag(tmp_path, "v1.1.0") == "v1.0.0"
