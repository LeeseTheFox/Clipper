"""Small custom widgets for icons that need presentation-time tinting."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Graphene, Gsk, Gtk


class TintedIcon(Gtk.Widget):
    """Render a bundled non-symbolic paintable through a solid color mask."""

    def __init__(self, icon_name: str, *, pixel_size: int = 16, color=None):
        super().__init__()
        self._paintable = Gtk.IconTheme.get_for_display(
            Gdk.Display.get_default()
        ).lookup_icon(
            icon_name,
            (),
            pixel_size,
            1,
            Gtk.TextDirection.NONE,
            0,
        )
        self._color = color or Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=1.0)
        self.set_size_request(pixel_size, pixel_size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_can_target(False)

    def do_snapshot(self, snapshot):
        if self._paintable is None:
            return

        width = max(1, self.get_width())
        height = max(1, self.get_height())
        size = min(width, height)
        offset_x = (width - size) / 2
        offset_y = (height - size) / 2
        snapshot.translate(Graphene.Point(x=offset_x, y=offset_y))
        bounds = Graphene.Rect().init(0, 0, size, size)

        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        self._paintable.snapshot(snapshot, size, size)
        snapshot.pop()
        snapshot.append_color(self._color, bounds)
        snapshot.pop()
