"""Protect localized README galleries when refreshing screenshot references."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools/generate_screenshots.py"
SPEC = importlib.util.spec_from_file_location("generate_screenshots", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
screenshots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(screenshots)


def test_refresh_preserves_translated_readme_text_and_is_repeatable():
    # A refresh must preserve contributor-written text and translated alt labels,
    # including on the second run when the screenshots already use <picture>.
    original = "Перед галереєю\n" + "\n".join(
        f'<p>\n  <img src="assets/screenshots/uk/{scene}-dark.png" '
        f'alt="Знімок {scene}" width="850">\n</p>'
        for scene in screenshots.SCENES
    ) + "\nПісля галереї\n"
    updated = screenshots.readme_with_themes(original, "uk")
    assert screenshots.readme_with_themes(updated, "uk") == updated
    assert updated.startswith("Перед галереєю\n")
    assert updated.endswith("\nПісля галереї\n")
    for scene in screenshots.SCENES:
        assert f'alt="Знімок {scene}" width="850"' in updated
        assert f'srcset="assets/screenshots/uk/{scene}-light.png"' in updated
        assert f'srcset="assets/screenshots/uk/{scene}-dark.png"' in updated
    assert updated.count("<picture>") == len(screenshots.SCENES)


def test_missing_screenshot_reference_aborts_readme_refresh():
    with pytest.raises(RuntimeError, match="Expected one games screenshot"):
        screenshots.readme_with_themes("Contributor documentation without a gallery", "en")
