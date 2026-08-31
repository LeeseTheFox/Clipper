"""Live, copyable application log window."""

from __future__ import annotations

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk


def is_scrolled_to_bottom(value: float, page_size: float, upper: float) -> bool:
    """Return whether a vertical adjustment is close enough to its bottom."""
    return value + page_size >= upper - 1


class LogWindow(Adw.Window):
    """Display an in-memory log buffer and keep it live while open."""

    def __init__(self, log_buffer, transient_for=None) -> None:
        super().__init__(title=_("Clipper logs"), transient_for=transient_for)
        self.set_default_size(800, 520)
        self._log_buffer = log_buffer
        self._update_pending = False
        self._idle_source_id = None
        self._destroyed = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy_button = Gtk.Button(label=_("Copy logs"))
        copy_button.connect("clicked", self._on_copy_logs)
        header.pack_end(copy_button)
        toolbar.add_top_bar(header)

        self._text_buffer = Gtk.TextBuffer()
        self._text_view = Gtk.TextView(buffer=self._text_buffer)
        self._text_view.set_editable(False)
        self._text_view.set_cursor_visible(False)
        self._text_view.set_monospace(True)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_top_margin(12)
        self._text_view.set_bottom_margin(12)
        self._text_view.set_left_margin(12)
        self._text_view.set_right_margin(12)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self._text_view)
        toolbar.set_content(self._scroller)
        self.set_content(toolbar)

        self._replace_text(follow=True)
        self._unsubscribe = self._log_buffer.subscribe(self._on_logs_changed)
        # Gtk.Window's default close only hides a window while another Python
        # object still owns it.  Clipper keeps this window on the application,
        # so explicitly destroy it instead of retaining and later re-presenting
        # a stale live-log subscription.
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

    def _on_close_request(self, *_args) -> bool:
        self._stop_live_updates()
        self.destroy()
        # We performed the close; suppress Gtk.Window's hide-only handler.
        return True

    def _on_destroy(self, *_args) -> None:
        self._stop_live_updates()

    def _stop_live_updates(self) -> None:
        """Disconnect the log listener and cancel any queued refresh once."""
        if self._destroyed:
            return
        self._destroyed = True
        unsubscribe = self._unsubscribe
        self._unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()
        if self._idle_source_id is not None:
            GLib.source_remove(self._idle_source_id)
            self._idle_source_id = None

    def _on_logs_changed(self) -> None:
        """Schedule a GTK-thread refresh for a buffer updated from any thread."""
        if self._destroyed or self._update_pending:
            return
        self._update_pending = True
        self._idle_source_id = GLib.idle_add(self._refresh_logs)

    def _refresh_logs(self) -> bool:
        self._update_pending = False
        self._idle_source_id = None
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        adjustment = self._scroller.get_vadjustment()
        follow = is_scrolled_to_bottom(
            adjustment.get_value(), adjustment.get_page_size(), adjustment.get_upper()
        )
        self._replace_text(follow=follow)
        return GLib.SOURCE_REMOVE

    def _replace_text(self, *, follow: bool) -> None:
        self._text_buffer.set_text(self._log_buffer.text())
        if follow:
            end = self._text_buffer.get_end_iter()
            self._text_view.scroll_to_iter(end, 0.0, False, 0.0, 1.0)

    def _on_copy_logs(self, _button) -> None:
        start = self._text_buffer.get_start_iter()
        end = self._text_buffer.get_end_iter()
        text = self._text_buffer.get_text(start, end, True)
        self.get_display().get_clipboard().set(text)
