import base64

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from clips_view import ClipsView
from gi.repository import Gdk, GLib, Gtk
from main_window import MainWindow


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


@pytest.fixture
def recording_preview(tmp_path, monkeypatch):
    def setup_preview_host(window):
        # Keep unrelated gallery discovery and navigation out of this GTK
        # layout check while using the production game-row builder.
        window.engine_status_indicator = Gtk.Box()
        window.set_content(window.engine_status_indicator)
        window.clips_view = ClipsView.__new__(ClipsView)
        Gtk.Box.__init__(window.clips_view)

    monkeypatch.setattr(MainWindow, "setup_ui", setup_preview_host)
    icon_path = tmp_path / "icon.png"
    texture = Gdk.MemoryTexture.new(
        256, 256, Gdk.MemoryFormat.R8G8B8A8,
        GLib.Bytes.new(bytes([255, 0, 0, 255]) * 256 * 256), 256 * 4,
    )
    texture.save_to_png(str(icon_path))
    entry = {"name": "Game", "icon_path": str(icon_path)}
    window = MainWindow(
        application=None, config={"output_folder": str(tmp_path)},
        recording_entry_callback=lambda: entry,
    )
    window._ensure_preview_popover()
    window._preview_active = True
    yield window, entry
    window._finish_preview_hide()
    window.destroy()


@pytest.mark.parametrize("width,height", [(360, 203), (114, 203), (360, 60)])
def test_long_recording_name_ellipsizes_inside_frame_without_resizing(
    recording_preview, width, height
):
    window, entry = recording_preview
    window._on_preview_frame(
        {
            "ok": True, "width": width, "height": height, "stride": width * 4,
            "data": base64.b64encode(bytes(width * height * 4)),
        },
        window._preview_generation,
    )
    window._update_preview_game()
    frame = window._preview_frame
    short_width = frame.measure(Gtk.Orientation.HORIZONTAL, -1)
    short_height = frame.measure(Gtk.Orientation.VERTICAL, width)

    # Unbroken Unicode text and embedded line breaks must not change geometry
    # or defeat ellipsization on portrait and ultrawide previews.
    entry["name"] = "遊戲Програма" * 300 + "\n" + "A" * 5000
    window._update_preview_game()

    assert frame.measure(Gtk.Orientation.HORIZONTAL, -1) == short_width
    assert frame.measure(Gtk.Orientation.VERTICAL, width) == short_height
    assert short_width.minimum == short_width.natural == width
    frame.allocate(width, short_height.natural, -1, None)
    label = next(widget for widget in _descendants(frame)
                 if isinstance(widget, Gtk.Label) and widget.get_text() == entry["name"])
    icon = next(widget for widget in _descendants(frame) if isinstance(widget, Gtk.Image))
    assert icon.get_paintable() is not None
    assert label.get_layout().is_ellipsized()
    assert label.get_layout().get_line_count() == 1
    for widget in (label, icon):
        success, bounds = widget.compute_bounds(frame)
        assert success
        assert 0 <= bounds.get_x() < bounds.get_x() + bounds.get_width() <= width
        assert height <= bounds.get_y() < bounds.get_y() + bounds.get_height()
        assert bounds.get_y() + bounds.get_height() <= frame.get_height()


def test_recording_preview_refreshes_identity_when_target_changes(recording_preview):
    window, entry = recording_preview
    window._request_preview_frame()
    first_row = window._preview_game_box
    window._request_preview_frame()
    assert window._preview_game_box is first_row

    entry["name"] = "Another app"
    window._request_preview_frame()
    labels = [widget.get_text() for widget in _descendants(window._preview_frame)
              if isinstance(widget, Gtk.Label)]
    assert "Another app" in labels
    assert "Game" not in labels

    entry.clear()
    window._request_preview_frame()
    assert window._preview_game_box is None
    assert window._preview_picture.get_parent() is not None
