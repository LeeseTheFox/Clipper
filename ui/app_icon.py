"""Registration for Clipper's application and bundled interface icons."""

from __future__ import annotations

from pathlib import Path

_APP_ICON_DIR = Path(__file__).resolve().parent / "icons/hicolor/scalable/apps"
_STEAM_ICON_DIR = Path(__file__).resolve().parent / "icons/hicolor/scalable/actions"
_FLATPAK_ICON_DIR = Path("/app/share/icons/hicolor/scalable/actions")
_UI_ICON_RESOURCE_FILE = (
    Path(__file__).resolve().parent / "icon-development-kit.gresource"
)
_UI_ICON_RESOURCE_PATH = "/io/github/leesethefox/Clipper/icons"
_ui_icon_resource = None


def register_app_icon() -> None:
    """Make Clipper's app and private interface icons available to GTK."""
    try:
        from gi.repository import Gdk, Gio, Gtk
    except (ImportError, ValueError):
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    theme = Gtk.IconTheme.get_for_display(display)
    for icon_dir_path in (_APP_ICON_DIR, _STEAM_ICON_DIR, _FLATPAK_ICON_DIR):
        if icon_dir_path.is_dir():
            icon_dir = str(icon_dir_path)
            if icon_dir not in theme.get_search_path():
                theme.add_search_path(icon_dir)

    global _ui_icon_resource
    if _ui_icon_resource is None and _UI_ICON_RESOURCE_FILE.is_file():
        _ui_icon_resource = Gio.Resource.load(str(_UI_ICON_RESOURCE_FILE))
        _ui_icon_resource._register()
    if _ui_icon_resource is not None:
        theme.add_resource_path(_UI_ICON_RESOURCE_PATH)
