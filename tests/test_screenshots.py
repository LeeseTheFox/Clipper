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
    # including migration from theme-aware galleries and subsequent refreshes.
    original = (
        "Перед галереєю\n# Clipper\n"
        + "\n".join(
            f"<p>\n  <picture>\n"
            f'    <source media="(prefers-color-scheme: light)" '
            f'srcset="assets/screenshots/uk/{scene}-light.png">\n'
            f'    <img src="assets/screenshots/uk/{scene}-dark.png" '
            f'alt="Знімок {scene}" width="850">\n  </picture>\n</p>'
            for scene in screenshots.SCENES
        )
        + "\nПісля галереї\n"
    )
    updated = screenshots.readme_with_generated_images(original, "uk")
    assert screenshots.readme_with_generated_images(updated, "uk") == updated
    assert updated.startswith("Перед галереєю\n")
    assert updated.count('src="assets/screenshots/uk/banner.png"') == 1
    assert updated.endswith("\nПісля галереї\n")
    for scene in screenshots.SCENES:
        assert f'alt="Знімок {scene}" width="850"' in updated
        assert f'src="assets/screenshots/uk/{scene}-dark.png"' in updated
    assert "-light.png" not in updated
    assert "<picture>" not in updated


def test_missing_screenshot_reference_aborts_readme_refresh():
    with pytest.raises(RuntimeError, match="Expected one games screenshot"):
        screenshots.readme_with_generated_images(
            "Contributor documentation without a gallery", "en"
        )


@pytest.mark.parametrize("heading", ["Mention # Clipper in prose", "# Clipper\n# Clipper"])
def test_missing_or_ambiguous_title_aborts_banner_insertion(heading):
    gallery = "\n".join(
        f'<img src="assets/screenshots/en/{scene}-dark.png" alt="{scene}">'
        for scene in screenshots.SCENES
    )
    with pytest.raises(RuntimeError, match="Expected one Clipper heading"):
        screenshots.readme_with_generated_images(heading + "\n" + gallery, "en")
