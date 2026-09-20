import gi

gi.require_version("Gtk", "4.0")

from editor_window import _EDITOR_CSS
from gi.repository import Gtk


def test_editor_css_parses_without_errors():
    errors = []
    provider = Gtk.CssProvider()
    provider.connect(
        "parsing-error",
        lambda _provider, section, error: errors.append((section, error)),
    )
    provider.load_from_string(_EDITOR_CSS)

    assert errors == []
