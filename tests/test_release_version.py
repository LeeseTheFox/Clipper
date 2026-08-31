from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "validate_release_version.py"
SPEC = importlib.util.spec_from_file_location("validate_release_version", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_version)


def write_release_metadata(root: Path, version: str) -> None:
    (root / "ui").mkdir()
    (root / "engine" / "src").mkdir(parents=True)
    (root / "packaging" / "flatpak").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "clipper"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "ui" / "main.py").write_text(
        f'about = Adw.AboutWindow(\n    version="{version}",\n)\n', encoding="utf-8"
    )
    (root / "engine" / "src" / "clipper_engine.c").write_text(
        f'#define ENGINE_VERSION   "{version}"\n', encoding="utf-8"
    )
    (root / "packaging" / "flatpak" / "io.github.leesethefox.Clipper.metainfo.xml").write_text(
        "<component><releases>"
        f'<release version="{version}" date="2026-08-30" />'
        "</releases></component>",
        encoding="utf-8",
    )


def test_validate_release_version_accepts_matching_semantic_tag(tmp_path):
    write_release_metadata(tmp_path, "1.2.3")

    assert release_version.validate_release_version(tmp_path, "v1.2.3") == "1.2.3"


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3-rc1"])
def test_validate_release_version_rejects_non_release_tags(tmp_path, tag):
    write_release_metadata(tmp_path, "1.2.3")

    with pytest.raises(ValueError, match="release tag"):
        release_version.validate_release_version(tmp_path, tag)


def test_validate_release_version_reports_each_mismatch(tmp_path):
    write_release_metadata(tmp_path, "1.2.3")
    (tmp_path / "ui" / "main.py").write_text(
        'about = Adw.AboutWindow(\n    version="1.2.2",\n)\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"ui/main.py About dialog is '1.2.2'"):
        release_version.validate_release_version(tmp_path, "v1.2.3")
