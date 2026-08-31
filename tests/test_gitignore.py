from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("engine", "packaging")


def test_flatpak_build_artifacts_are_ignored():
    root_gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "build-dir/" in root_gitignore
    assert "build-dir-bounded/" in root_gitignore
    assert "build-dir-download/" in root_gitignore
    assert "repo/" in root_gitignore
    assert ".flatpak-builder/" in root_gitignore


def test_engine_build_artifacts_are_ignored():
    engine_gitignore = (
        REPO_ROOT / "engine" / "src" / ".gitignore"
    ).read_text(encoding="utf-8")
    monitor_gitignore = (
        REPO_ROOT / "engine" / "monitor" / ".gitignore"
    ).read_text(encoding="utf-8")

    assert "clipper-engine" in engine_gitignore
    assert "obs-ffmpeg-mux" in engine_gitignore
    assert "clipper-monitor-host" in monitor_gitignore


def test_source_tree_has_no_python_cache_directories():
    cache_dirs = [
        path.relative_to(REPO_ROOT)
        for source_dir in SOURCE_DIRS
        for path in (REPO_ROOT / source_dir).rglob("__pycache__")
    ]

    assert cache_dirs == []
