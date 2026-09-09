import importlib.util
import json
from pathlib import Path

import pytest


def load_publisher():
    path = Path(__file__).resolve().parents[1] / "tools/publish_github_release.py"
    spec = importlib.util.spec_from_file_location("update_publisher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("newer,latest", [(False, "--latest=true"), (True, "--latest=false")])
def test_release_is_published_only_after_all_assets(monkeypatch, newer, latest):
    module = load_publisher()
    for key, value in {
        "TAG": "v1.2.0",
        "VERSION": "1.2.0",
        "BUNDLE": "versioned.flatpak",
        "GITHUB_REPOSITORY": "LeeseTheFox/Clipper",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def gh(*args):
        calls.append(args)
        if args[0] == "api":
            return json.dumps(
                [
                    [
                        {
                            "tag_name": "v1.10.0" if newer else "v1.1.0",
                            "draft": False,
                            "prerelease": False,
                        }
                    ]
                ]
            )
        return ""

    monkeypatch.setattr(module, "gh", gh)
    module.main()
    assert "--draft" in calls[1]
    assert calls[2][:2] == ("release", "upload")
    assert "update-x86_64.json" in calls[2]
    assert "Clipper-x86_64.flatpak" in calls[2]
    assert "--draft=false" in calls[3]
    assert latest in calls[3]


def test_published_release_cannot_be_overwritten(monkeypatch):
    module = load_publisher()
    for key, value in {
        "TAG": "v1.2.0",
        "VERSION": "1.2.0",
        "BUNDLE": "bundle.flatpak",
        "GITHUB_REPOSITORY": "LeeseTheFox/Clipper",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps([[{"tag_name": "v1.2.0", "draft": False, "prerelease": False}]])

    monkeypatch.setattr(module, "gh", gh)
    with pytest.raises(RuntimeError, match="already public"):
        module.main()
    assert len(calls) == 1
