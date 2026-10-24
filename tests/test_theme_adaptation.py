import gi
import pytest

gi.require_version("Gtk", "4.0")

from editor_window import _EDITOR_CSS
from gi.repository import Gtk
from playback_performance import PERFORMANCE_CSS


@pytest.mark.parametrize("css", [_EDITOR_CSS, PERFORMANCE_CSS], ids=["editor", "performance"])
def test_video_ui_css_parses_without_errors(css):
    errors = []
    provider = Gtk.CssProvider()
    provider.connect(
        "parsing-error",
        lambda _provider, section, error: errors.append((section, error)),
    )
    provider.load_from_string(css)

    assert errors == []
