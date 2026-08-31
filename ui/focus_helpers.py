"""Small GTK selection-widget helpers shared by UI views."""

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk
from text_helpers import ELLIPSIZE_END, configure_single_line_ellipsis, set_single_line_label_text


def new_id_dropdown(
    options,
    active_id=None,
    *,
    max_width_chars: int | None = None,
    width_request: int | None = None,
) -> Gtk.DropDown:
    """Create a Gtk.DropDown that stores stable IDs beside display labels."""
    labels = [label for label, _option_id in options]
    option_ids = [option_id for _label, option_id in options]

    dropdown = Gtk.DropDown.new_from_strings(labels)
    if width_request is not None:
        dropdown.set_size_request(width_request, -1)
    if max_width_chars is not None:
        dropdown.set_factory(_new_dropdown_label_factory(max_width_chars))
        dropdown.set_list_factory(_new_dropdown_label_factory(max_width_chars))
    dropdown._clipper_option_ids = option_ids
    dropdown.set_focus_on_click(False)

    selected = option_ids.index(active_id) if active_id in option_ids else 0
    dropdown.set_selected(selected)

    _consume_widget_scroll(dropdown)
    dropdown.connect("notify::selected", _clear_widget_window_focus_later)
    return dropdown


def _new_dropdown_label_factory(max_width_chars: int):
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0)
        configure_single_line_ellipsis(
            label,
            mode=ELLIPSIZE_END,
            max_width_chars=max_width_chars,
        )
        list_item.set_child(label)

    def bind(_factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        text = item.get_string() if item is not None else ""
        set_single_line_label_text(label, text)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def dropdown_active_id(dropdown: Gtk.DropDown, default=None):
    """Return the stable ID for a Gtk.DropDown selection."""
    option_ids = getattr(dropdown, "_clipper_option_ids", ())
    selected = dropdown.get_selected()
    if 0 <= selected < len(option_ids):
        return option_ids[selected]
    return default


def _consume_widget_scroll(widget: Gtk.Widget) -> None:
    flags = Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.HORIZONTAL
    controller = Gtk.EventControllerScroll.new(flags)
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    controller.connect("scroll", lambda _controller, _dx, _dy: True)
    widget.add_controller(controller)


def _clear_widget_window_focus_later(widget: Gtk.Widget, *_unused) -> None:
    def clear_focus():
        root = widget.get_root()
        if root is not None and hasattr(root, "set_focus"):
            root.set_focus(None)
            if hasattr(root, "present"):
                root.present()
        return False

    GLib.idle_add(clear_focus)
