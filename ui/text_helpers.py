"""Small helpers for displaying dynamic UI text safely."""

from html import escape

try:
    from gi.repository import Pango

    ELLIPSIZE_END = Pango.EllipsizeMode.END
    ELLIPSIZE_MIDDLE = Pango.EllipsizeMode.MIDDLE
except (ImportError, AttributeError):
    # Pango enum values: NONE=0, START=1, MIDDLE=2, END=3.
    ELLIPSIZE_MIDDLE = 2
    ELLIPSIZE_END = 3


def configure_single_line_ellipsis(
    label,
    *,
    mode=ELLIPSIZE_MIDDLE,
    tooltip_text: str | None = None,
    max_width_chars: int | None = None,
) -> None:
    """Make a Gtk.Label shrink before it asks the window to grow."""
    for method_name, args in (
        ("set_hexpand", (True,)),
        ("set_width_chars", (1,)),
        ("set_ellipsize", (mode,)),
        ("set_single_line_mode", (True,)),
    ):
        method = getattr(label, method_name, None)
        if method is not None:
            method(*args)

    if max_width_chars is not None:
        method = getattr(label, "set_max_width_chars", None)
        if method is not None:
            method(max_width_chars)

    if tooltip_text is None:
        getter = getattr(label, "get_text", None) or getattr(label, "get_label", None)
        tooltip_text = getter() if getter is not None else None

    set_tooltip = getattr(label, "set_tooltip_text", None)
    if set_tooltip is not None:
        set_tooltip(tooltip_text or None)


def set_single_line_label_text(label, text: str, *, tooltip: bool = True) -> None:
    """Set label text and keep its tooltip in sync with the full string."""
    label.set_text(text)
    if tooltip:
        set_tooltip = getattr(label, "set_tooltip_text", None)
        if set_tooltip is not None:
            set_tooltip(text or None)


def middle_truncate_text(text: object, max_chars: int = 80) -> str:
    """Return a bounded middle-truncated string for widgets without label access."""
    value = str(text or "")
    if max_chars < 5 or len(value) <= max_chars:
        return value

    keep = max_chars - 3
    head = keep // 2
    tail = keep - head
    return f"{value[:head]}...{value[-tail:]}"


def escape_markup_text(text: object) -> str:
    """Escape plain text before passing it to GTK/Pango markup-aware APIs."""
    return escape(str(text or ""), quote=False)
