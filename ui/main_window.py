"""
Main window for Clipper application
"""

import base64

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from clips_view import ClipsView
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk
from icon_names import AUDIO, CLIPS, GAME, HAMBURGER_MENU
from text_helpers import escape_markup_text
from window_state import WindowSizeManager


class MainWindow(Adw.ApplicationWindow):
    """Main application window with navigation between different views"""

    DEFAULT_WIDTH = 800
    DEFAULT_HEIGHT = 600
    MINIMUM_WIDTH = 512
    MINIMUM_HEIGHT = 500
    COMPACT_WIDTH = 600

    ENGINE_STATUS_READY = "ready"
    ENGINE_STATUS_RECORDING = "recording"
    ENGINE_STATUS_EXITED = "exited"
    ENGINE_STATUS_CRASHED = "crashed"
    ENGINE_STATUS_STOPPED = ENGINE_STATUS_READY
    ENGINE_STATUS_RUNNING = ENGINE_STATUS_READY

    _ENGINE_STATUS_LABELS = {
        ENGINE_STATUS_READY: _("Ready"),
        ENGINE_STATUS_RECORDING: _("Recording"),
        ENGINE_STATUS_EXITED: _("Exited"),
        ENGINE_STATUS_CRASHED: _("Crashed"),
    }

    _ENGINE_STATUS_STYLE_CLASSES = {
        ENGINE_STATUS_READY: "ready",
        ENGINE_STATUS_RECORDING: "recording",
        ENGINE_STATUS_EXITED: "exited",
        ENGINE_STATUS_CRASHED: "crashed",
    }
    _PREVIEW_MAX_WIDTH = 360
    _PREVIEW_MAX_HEIGHT = 203
    _PREVIEW_INTERVAL_MS = 250
    _PREVIEW_FADE_MS = 160

    def __init__(
        self,
        application,
        config=None,
        engine_client=None,
        hotkey_changed_callback=None,
        hotkey_capture_state_callback=None,
        capabilities_requested_callback=None,
        capabilities_finished_callback=None,
        engine_restart_required_callback=None,
        engine_restart_requested_callback=None,
        start_on_boot_changed_callback=None,
        display_target_change_callback=None,
        show_display_target_controls=True,
        tray_available_callback=None,
        capabilities_cache=None,
        edit_clip_callback=None,
        presentation_callback=None,
        language_changed_callback=None,
    ):
        super().__init__(application=application, title=_("Clipper"))

        self._config = config
        self._engine_client = engine_client
        self._hotkey_changed_callback = hotkey_changed_callback
        self._hotkey_capture_state_callback = hotkey_capture_state_callback
        self._capabilities_requested_callback = capabilities_requested_callback
        self._capabilities_finished_callback = capabilities_finished_callback
        self._engine_restart_required_callback = engine_restart_required_callback
        self._engine_restart_requested_callback = engine_restart_requested_callback
        self._start_on_boot_changed_callback = start_on_boot_changed_callback
        self._display_target_change_callback = display_target_change_callback
        self._show_display_target_controls = bool(show_display_target_controls)
        self._tray_available_callback = tray_available_callback
        self._capabilities_cache = capabilities_cache
        self._edit_clip_callback = edit_clip_callback
        self._presentation_callback = presentation_callback
        self._language_changed_callback = language_changed_callback
        self._current_toast = None
        self._engine_status = None
        self._preview_popover = None
        self._preview_picture = None
        self._preview_placeholder = None
        self._preview_timer_id = None
        self._preview_fade_id = None
        self._preview_in_flight = False
        self._preview_active = False
        self._preview_generation = 0
        self._preferences_dialog = None

        # Keep the established default size while allowing a narrow, phone-like
        # layout. Navigation moves from the header to the bottom at 600sp.
        self.set_default_size(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.set_size_request(self.MINIMUM_WIDTH, self.MINIMUM_HEIGHT)
        self._window_size = WindowSizeManager(
            self,
            self._config,
            "main",
            minimum_size=(self.MINIMUM_WIDTH, self.MINIMUM_HEIGHT),
        )

        self._load_css()

        # Create main layout
        self.setup_ui()

    def setup_ui(self):
        """Build the main window UI"""
        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        self.toolbar_view.set_bottom_bar_style(Adw.ToolbarStyle.FLAT)
        self.toolbar_view.set_reveal_bottom_bars(False)

        # Toast overlay for notifications within the active page.
        self.toast_overlay = Adw.ToastOverlay()

        # Create header bar with view switcher
        self.header = Adw.HeaderBar()

        self.engine_status_indicator = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.engine_status_indicator.add_css_class("engine-status-indicator")
        self.engine_status_indicator.set_tooltip_text(_("Engine status"))
        status_motion = Gtk.EventControllerMotion()
        status_motion.connect("enter", self._on_status_hover_enter)
        status_motion.connect("leave", self._on_status_hover_leave)
        self.engine_status_indicator.add_controller(status_motion)

        self.engine_status_dot = Gtk.DrawingArea()
        self.engine_status_dot.add_css_class("engine-status-dot")
        self.engine_status_dot.set_content_width(8)
        self.engine_status_dot.set_content_height(8)
        self.engine_status_dot.set_size_request(8, 8)
        self.engine_status_dot.set_valign(Gtk.Align.CENTER)
        self.engine_status_dot.set_draw_func(self._draw_engine_status_dot)

        self.engine_status_label = Gtk.Label()
        self.engine_status_label.add_css_class("caption")
        self.engine_status_label.set_xalign(0)

        self.engine_status_indicator.append(self.engine_status_dot)
        self.engine_status_indicator.append(self.engine_status_label)
        self.header.pack_start(self.engine_status_indicator)
        self.set_engine_status(self.ENGINE_STATUS_READY)

        # View switcher for navigation
        self.header_view_switcher = Adw.ViewSwitcher()
        self.header_view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self.header.set_title_widget(self.header_view_switcher)

        # Menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name(HAMBURGER_MENU)
        menu_button.set_tooltip_text(_("Main menu"))
        menu = Gio.Menu()
        menu.append(_("Preferences"), "win.preferences")
        menu.append(_("Check for updates"), "app.check-updates")
        debug_menu = Gio.Menu()
        debug_menu.append(_("Show logs"), "app.show-logs")
        if self._show_display_target_controls:
            debug_menu.append(
                _("Clear PipeWire restore token"), "app.clear-pipewire-restore-token"
            )
        debug_menu.append(_("Reset config"), "app.reset-config")
        debug_menu.append(_("Reset to factory settings"), "app.reset-factory-settings")
        debug_menu.append(_("Restart engine"), "app.restart-engine")
        menu.append_submenu(_("Debug menu"), debug_menu)
        menu.append(_("About Clipper"), "app.about")
        menu.append(_("Quit"), "app.quit")
        menu_button.set_menu_model(menu)
        self.header.pack_end(menu_button)

        preferences_action = Gio.SimpleAction.new("preferences", None)
        preferences_action.connect("activate", self._on_preferences_activated)
        self.add_action(preferences_action)

        self.toolbar_view.add_top_bar(self.header)
        self.update_banner = Adw.Banner(title=_("A Clipper update is available"),
                                        button_label=_("View update"), revealed=False)
        self.update_banner.connect("button-clicked", lambda _banner:
                                   self.get_application().activate_action("view-update", None))
        self.toolbar_view.add_top_bar(self.update_banner)

        # Create view stack for different sections
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_hhomogeneous(False)
        self.header_view_switcher.set_stack(self.view_stack)

        # Add views — pass config where applicable
        self.clips_view = ClipsView(
            config=self._config,
            edit_clip_callback=self._edit_clip_callback,
            clips_rendered_callback=lambda: self._notify_presentable("clips"),
        )
        self.view_stack.add_titled_with_icon(self.clips_view, "clips", _("Clips"), CLIPS)
        self._clips_shift_keycodes = set()
        clips_key_controller = Gtk.EventControllerKey()
        clips_key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        clips_key_controller.connect("key-pressed", self._on_clips_key_pressed)
        clips_key_controller.connect("key-released", self._on_clips_key_released)
        self.add_controller(clips_key_controller)
        self.connect("notify::is-active", self._on_window_active_changed)

        self.audio_view = None
        self.whitelist_view = None
        self.settings_view = None

        self._whitelist_container = Gtk.Box()
        self.view_stack.add_titled_with_icon(
            self._whitelist_container, "whitelist", _("Games"), GAME
        )

        self._audio_container = Gtk.Box()
        self.view_stack.add_titled_with_icon(
            self._audio_container, "audio", _("Audio"), AUDIO
        )
        self.toast_overlay.set_child(self.view_stack)
        self.toolbar_view.set_content(self.toast_overlay)

        self.bottom_view_switcher = Adw.ViewSwitcherBar()
        self.bottom_view_switcher.set_stack(self.view_stack)
        self.bottom_view_switcher.set_reveal(True)
        self.toolbar_view.add_bottom_bar(self.bottom_view_switcher)

        self._compact_breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(f"max-width: {self.COMPACT_WIDTH}sp")
        )
        self._add_breakpoint_setter(self._compact_breakpoint, self.header, "title-widget", None)
        self._add_breakpoint_setter(
            self._compact_breakpoint,
            self.toolbar_view,
            "reveal-bottom-bars",
            True,
        )
        self.add_breakpoint(self._compact_breakpoint)

        # Clear focus from clips view search entry when switching tabs
        self.view_stack.connect("notify::visible-child", self._on_view_stack_changed)

        self.set_content(self.toolbar_view)

    @staticmethod
    def _add_breakpoint_setter(breakpoint, widget, property_name: str, value) -> None:
        """Add a typed libadwaita breakpoint setter from a Python value."""
        property_spec = widget.find_property(property_name)
        property_value = GObject.Value()
        property_value.init(property_spec.value_type)
        property_value.set_value(value)
        breakpoint.add_setter(widget, property_name, property_value)

    def _notify_presentable(self, content: str) -> None:
        """Report when content is ready to be included in the next window frame."""
        if self._presentation_callback is not None:
            self._presentation_callback(content)

    def reload_from_config(self) -> None:
        """Refresh every configuration-backed view after a config reset."""
        if self._config is None:
            return

        self.clips_view.on_output_folder_changed(
            self._config.get("output_folder", "~/Videos/Clipper")
        )
        # Each view is explicitly reloaded below.  Do not emit the normal
        # whitelist-changed callback here: during a factory reset it could let
        # AudioTracksView persist its pre-reset in-memory state over DEFAULTS.
        if self.whitelist_view is not None:
            self.whitelist_view.reload_from_config(notify=False)
        if self.audio_view is not None:
            self.audio_view.reload_from_config()
        if self.settings_view is not None:
            self.settings_view.reload_from_config()

    def cleanup(self) -> None:
        """Stop child-view background work before the application exits."""
        for view in (
            self.clips_view,
            self.audio_view,
            self.settings_view,
            self.whitelist_view,
        ):
            cleanup = getattr(view, "cleanup", None)
            if callable(cleanup):
                cleanup()

    def _on_view_stack_changed(self, stack, param):
        """Clear focus when switching tabs to prevent unwanted focus transfer."""
        # Clear focus from the window to prevent focus from previous view
        # transferring to the first widget in the new view
        self.set_focus(None)
        visible_name = stack.get_visible_child_name()
        self._set_clips_shift_edit_mode(
            visible_name == "clips" and bool(self._clips_shift_keycodes)
        )
        self._ensure_view(visible_name)
        if self.audio_view is not None:
            self.audio_view.set_active(visible_name == "audio")

    def _on_clips_key_pressed(self, _controller, keyval, keycode, _state):
        if keyval not in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R):
            return False
        self._clips_shift_keycodes.add(keycode)
        if self.view_stack.get_visible_child_name() == "clips":
            self._set_clips_shift_edit_mode(True)
        return False

    def _on_clips_key_released(self, _controller, keyval, keycode, _state):
        if keyval not in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R):
            return
        self._clips_shift_keycodes.discard(keycode)
        self._set_clips_shift_edit_mode(
            self.view_stack.get_visible_child_name() == "clips"
            and bool(self._clips_shift_keycodes)
        )

    def _on_window_active_changed(self, window, _param):
        if window.is_active():
            return
        self._clips_shift_keycodes.clear()
        self._set_clips_shift_edit_mode(False)

    def _set_clips_shift_edit_mode(self, active):
        setter = getattr(self.clips_view, "set_shift_edit_mode", None)
        if callable(setter):
            setter(active)

    def _ensure_view(self, name: str | None) -> None:
        """Construct a page once, either at startup or when first opened."""
        if name == "whitelist" and self.whitelist_view is None:
            from whitelist_view import WhitelistView

            self.whitelist_view = WhitelistView(
                config=self._config,
                whitelist_changed_callback=self._on_whitelist_changed,
            )
            self._whitelist_container.append(self.whitelist_view)
        elif name == "audio" and self.audio_view is None:
            from audio_tracks_view import AudioTracksView

            self.audio_view = AudioTracksView(
                config=self._config,
                engine_client=self._engine_client,
                capabilities_requested_callback=self._capabilities_requested_callback,
                capabilities_finished_callback=self._capabilities_finished_callback,
                engine_restart_required_callback=self._engine_restart_required_callback,
                engine_restart_requested_callback=self._engine_restart_requested_callback,
            )
            self._audio_container.append(self.audio_view)

    def _on_preferences_activated(self, _action, _parameter) -> None:
        self.show_preferences()

    def show_preferences(self) -> None:
        """Present the reusable preferences dialog, constructing it on demand."""
        if self._preferences_dialog is None:
            from preferences_dialog import PreferencesDialog
            from settings_view import SettingsView

            self.settings_view = SettingsView(
                config=self._config,
                engine_client=self._engine_client,
                output_folder_changed_callback=self.clips_view.on_output_folder_changed,
                hotkey_changed_callback=self._hotkey_changed_callback,
                hotkey_capture_state_callback=self._hotkey_capture_state_callback,
                capabilities_requested_callback=self._capabilities_requested_callback,
                capabilities_finished_callback=self._capabilities_finished_callback,
                engine_restart_required_callback=self._engine_restart_required_callback,
                start_on_boot_changed_callback=self._start_on_boot_changed_callback,
                display_target_change_callback=self._display_target_change_callback,
                show_display_target_controls=self._show_display_target_controls,
                tray_available_callback=self._tray_available_callback,
                capabilities_cache=self._capabilities_cache,
                language_changed_callback=lambda language: self._language_changed_callback(
                    language, self._preferences_dialog, reopen_preferences=True
                )
                if self._language_changed_callback is not None
                else None,
            )
            self._preferences_dialog = PreferencesDialog(self.settings_view)

        self._preferences_dialog.present(self)

    def _on_whitelist_changed(self) -> None:
        if self.audio_view is not None:
            self.audio_view.on_whitelist_changed()

    def show_toast(self, message):
        """Show a toast notification"""
        if self._current_toast is not None:
            self._current_toast.dismiss()

        toast = Adw.Toast.new(escape_markup_text(message))
        toast.set_timeout(3)
        toast.connect("dismissed", self._on_toast_dismissed)
        self._current_toast = toast
        self.toast_overlay.add_toast(toast)

    def _on_toast_dismissed(self, toast):
        """Forget the active toast once libadwaita has dismissed it."""
        if toast is self._current_toast:
            self._current_toast = None

    def set_engine_status(self, status: str) -> None:
        """Update the header-bar engine status indicator."""
        if status not in self._ENGINE_STATUS_LABELS:
            status = self.ENGINE_STATUS_READY
        if status == self._engine_status:
            return

        self._engine_status = status
        self.engine_status_label.set_label(self._ENGINE_STATUS_LABELS[status])
        if status == self.ENGINE_STATUS_RECORDING:
            self.engine_status_indicator.set_tooltip_text(None)
        else:
            self.engine_status_indicator.set_tooltip_text(_("Engine status"))

        for style_class in self._ENGINE_STATUS_STYLE_CLASSES.values():
            self.engine_status_indicator.remove_css_class(style_class)

        self.engine_status_indicator.add_css_class(self._ENGINE_STATUS_STYLE_CLASSES[status])
        self.engine_status_dot.queue_draw()
        if status != self.ENGINE_STATUS_RECORDING:
            self._hide_preview_popover()

    def _on_status_hover_enter(self, _controller, _x: float, _y: float) -> None:
        if self._engine_status != self.ENGINE_STATUS_RECORDING:
            return
        if not self._engine_client or not self._engine_client.is_connected():
            return
        self._show_preview_popover()

    def _on_status_hover_leave(self, _controller) -> None:
        self._hide_preview_popover()

    def _ensure_preview_popover(self) -> None:
        if self._preview_popover is not None:
            return

        popover = Gtk.Popover()
        popover.add_css_class("recording-preview-popover")
        popover.set_has_arrow(False)
        popover.set_autohide(False)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_parent(self.engine_status_indicator)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("recording-preview-frame")

        overlay = Gtk.Overlay()
        picture = Gtk.Picture()
        picture.add_css_class("recording-preview-picture")
        picture.set_size_request(self._PREVIEW_MAX_WIDTH, self._PREVIEW_MAX_HEIGHT)
        if hasattr(Gtk, "ContentFit"):
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)

        placeholder = Gtk.Label(label=_("Waiting for frame"))
        placeholder.add_css_class("dim-label")
        placeholder.add_css_class("caption")
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)

        overlay.set_child(picture)
        overlay.add_overlay(placeholder)
        frame.append(overlay)
        popover.set_child(frame)

        self._preview_popover = popover
        self._preview_picture = picture
        self._preview_placeholder = placeholder

    def _show_preview_popover(self) -> None:
        self._preview_active = True
        self._preview_generation += 1
        self._ensure_preview_popover()
        if self._preview_popover is None:
            return

        if self._preview_fade_id is not None:
            GLib.source_remove(self._preview_fade_id)
            self._preview_fade_id = None

        self._preview_popover.set_opacity(1.0)
        self._preview_popover.popup()
        self._request_preview_frame()
        if self._preview_timer_id is None:
            self._preview_timer_id = GLib.timeout_add(
                self._PREVIEW_INTERVAL_MS, self._on_preview_timer
            )

    def _hide_preview_popover(self) -> None:
        self._preview_active = False
        self._preview_generation += 1
        self._preview_in_flight = False

        if self._preview_timer_id is not None:
            GLib.source_remove(self._preview_timer_id)
            self._preview_timer_id = None

        if self._preview_popover is None:
            return

        self._preview_popover.set_opacity(0.0)
        if self._preview_fade_id is not None:
            GLib.source_remove(self._preview_fade_id)
        self._preview_fade_id = GLib.timeout_add(self._PREVIEW_FADE_MS, self._finish_preview_hide)

    def _finish_preview_hide(self) -> bool:
        self._preview_fade_id = None
        if self._preview_popover is not None:
            self._preview_popover.popdown()
            self._preview_popover.unparent()
        if self._preview_picture is not None:
            self._preview_picture.set_paintable(None)
        self._preview_popover = None
        self._preview_picture = None
        self._preview_placeholder = None
        return False

    def _on_preview_timer(self) -> bool:
        if not self._preview_active or self._engine_status != self.ENGINE_STATUS_RECORDING:
            self._preview_timer_id = None
            return False
        self._request_preview_frame()
        return True

    def _request_preview_frame(self) -> None:
        if not self._preview_active:
            return
        if self._preview_in_flight:
            return
        if not self._engine_client or not self._engine_client.is_connected():
            return

        generation = self._preview_generation
        self._preview_in_flight = self._engine_client.get_preview_frame(
            lambda response: self._on_preview_frame(response, generation),
            width=self._PREVIEW_MAX_WIDTH,
            height=self._PREVIEW_MAX_HEIGHT,
            preserve_aspect=True,
        )

    def _on_preview_frame(self, response: dict, generation: int) -> None:
        self._preview_in_flight = False
        if not self._preview_active or generation != self._preview_generation:
            return

        if not response.get("ok"):
            if self._preview_placeholder is not None:
                self._preview_placeholder.set_label(_("Preview unavailable"))
                self._preview_placeholder.set_visible(True)
            return

        try:
            width = int(response["width"])
            height = int(response["height"])
            stride = int(response["stride"])
            frame_data = base64.b64decode(response["data"], validate=True)
        except (KeyError, TypeError, ValueError):
            return

        if width <= 0 or height <= 0 or stride < width * 4:
            return
        if len(frame_data) < stride * height:
            return

        texture = Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.B8G8R8A8,
            GLib.Bytes.new(frame_data),
            stride,
        )
        if self._preview_picture is not None:
            self._preview_picture.set_size_request(width, height)
            self._preview_picture.set_paintable(texture)
        if self._preview_placeholder is not None:
            self._preview_placeholder.set_visible(False)

    def _draw_engine_status_dot(self, area, cr, width: int, height: int) -> None:
        """Draw a fixed circular marker using the current status color."""
        if width <= 0 or height <= 0:
            return

        color = self.engine_status_indicator.get_style_context().get_color()
        radius = min(width, height) / 2
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        cr.arc(width / 2, height / 2, radius, 0, 6.283185307179586)
        cr.fill()

    def _load_css(self) -> None:
        """Load small app-local styles for the engine status indicator."""
        display = Gdk.Display.get_default()
        if display is None:
            return

        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            .engine-status-indicator {
                border-radius: 999px;
                padding: 4px 10px;
                background-color: alpha(currentColor, 0.08);
            }

            .engine-status-dot {
                margin-top: 2px;
                margin-bottom: 2px;
            }

            .engine-status-indicator.ready {
                color: @success_color;
            }

            .engine-status-indicator.recording {
                color: @error_color;
            }

            .engine-status-indicator.exited {
                color: @warning_color;
            }

            .engine-status-indicator.crashed {
                color: @error_color;
            }

            .recording-preview-popover {
                opacity: 1;
                transition: opacity 160ms ease-out;
            }

            .recording-preview-frame {
                padding: 0;
                background: #111111;
                border-radius: 8px;
                box-shadow: 0 8px 24px alpha(black, 0.32);
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
