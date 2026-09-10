#!/usr/bin/env python3
"""
Clipper - Game clip recorder for Linux
Main application entry point
"""

import json
import os
import platform
import sys
import threading
import time
import weakref
from typing import Any, cast

import i18n
from i18n import _

i18n.bootstrap()

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from app_icon import register_app_icon
from audio_source_discovery import list_playback_audio_sources
from autostart import AutostartManager
from capture_modes import (
    CAPTURE_MODE_DISPLAY,
    CAPTURE_MODE_GAME,
    DEFAULT_CAPTURE_MODE,
    capture_mode_for_entry,
)
from config import ClipperConfig, normalize_clip_sound_volume
from display_auth import normalize_display_auth_env
from engine_client import EngineClient
from engine_manager import RESTART_EXIT_CODE, EngineProcessManager
from game_audio_learning import (
    apply_learned_game_audio_identity,
    audio_uses_whitelisted_game,
    format_game_audio_probe,
    probe_game_audio_identity,
)
from gi.repository import Adw, Gio, GLib, Gtk
from hotkeys import (
    GlobalHotkeyManager,
    display_hotkey,
    effective_hotkey_label,
    shortcut_id_for_hotkey,
)
from icon_names import APP_ICON
from log_window import LogWindow
from logs import LogBuffer, consume_handoff, create_handoff
from monitor_ipc import MonitorIpcServer, default_monitor_socket_path
from monitor_manager import HostMonitorManager
from process_watcher import (
    find_running_obs_studio,
    find_running_whitelist_entry,
    monitor_rule_id,
)
from sound_feedback import ClipSoundPlayer
from tray import ClipperTray

if hasattr(Gtk, "Widget"):
    Gtk.Widget.set_default_direction(
        Gtk.TextDirection.RTL
        if i18n.text_direction() == "rtl"
        else Gtk.TextDirection.LTR
    )

SetupWindow = None
MainWindow = None

_BACKGROUND_OPTION = "--background"
_LOG_HANDOFF_FD_ENV = "CLIPPER_INTERNAL_LOG_HANDOFF_FD"
_CAPABILITIES_HANDOFF_ENV = "CLIPPER_INTERNAL_CAPABILITIES_HANDOFF"
_REOPEN_PREFERENCES_ENV = "CLIPPER_INTERNAL_REOPEN_PREFERENCES"
_ENGINE_STATUS_READY = "ready"
_ENGINE_STATUS_RECORDING = "recording"
_ENGINE_STATUS_EXITED = "exited"
_ENGINE_STATUS_CRASHED = "crashed"
_OBS_STUDIO_MONITOR_RULE_ID = "clipper-observer-obs-studio"
_OBS_CONFLICT_NOTIFICATION_ID = "obs-studio-conflict"
_OBS_CONFLICT_TITLE = _("OBS Studio and Clipper are both running")
_OBS_CONFLICT_BODY = (
    _(
        "Running both apps at the same time can overload the GPU or hardware encoder. "
        'This may also cause issues for games with "Game capture" method selected. '
        "It is advised to close OBS Studio or Clipper before recording."
    )
)
_HOTKEY_CAPTURE_RELEASE_DELAY_MS = 500
_EXPORT_PROBE_DELAY_SECONDS = 3
_PRESENTATION_ACTION = "presentation-state"
_VERSION_EASTER_EGG_CLICKS = 5
_VERSION_EASTER_EGG_INTERVAL_SECONDS = 0.7


def _is_x11_session() -> bool:
    """Return whether the desktop session uses X11 rather than Wayland."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if session_type:
        return session_type == "x11"
    return bool(os.environ.get("DISPLAY")) and not bool(os.environ.get("WAYLAND_DISPLAY"))


def _application_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Remove Clipper's private background option before GApplication parsing."""
    start_hidden = _BACKGROUND_OPTION in argv[1:]
    return [argument for argument in argv if argument != _BACKGROUND_OPTION], start_hidden


def _initial_log_buffer() -> LogBuffer:
    """Restore logs from an intentional background-process handoff, if any."""
    inherited_fd = os.environ.pop(_LOG_HANDOFF_FD_ENV, None)
    if inherited_fd is None:
        return LogBuffer()
    try:
        return consume_handoff(int(inherited_fd))
    except (OSError, TypeError, ValueError) as error:
        logs = LogBuffer()
        logs.add(f"[clipper] WARN: could not restore earlier logs: {error}")
        return logs


def _initial_capabilities_cache() -> dict:
    """Restore a capability probe across an intentional background handoff."""
    serialized = os.environ.pop(_CAPABILITIES_HANDOFF_ENV, None)
    if serialized is None:
        return {}
    try:
        cache = json.loads(serialized)
    except (json.JSONDecodeError, TypeError):
        return {}
    return cache if isinstance(cache, dict) else {}


class ClipperApplication(Adw.Application):
    """Main application class for Clipper"""

    def __init__(
        self,
        *,
        start_hidden: bool = False,
        log_buffer: LogBuffer | None = None,
        capabilities_cache: dict | None = None,
    ):
        super().__init__(
            application_id="io.github.leesethefox.Clipper",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        # Capture the session type once so UI visibility does not change if
        # the environment is altered while Clipper is running.
        self._is_x11 = _is_x11_session()
        self.window: Any | None = None
        self.editor_window: Any | None = None
        self.setup_window: Any | None = None
        self._start_hidden = start_hidden
        self._background_hold = False
        self._restart_in_background = False
        self._restart_in_foreground = False
        self._reopen_preferences = os.environ.pop(_REOPEN_PREFERENCES_ENV, "") == "1"
        self._editor_logout_inhibit_cookie = 0
        self._session_end_prompt_pending = False
        self._capabilities_cache = capabilities_cache if capabilities_cache is not None else {}
        self._config = ClipperConfig()
        self._log_buffer = log_buffer if log_buffer is not None else LogBuffer()
        self._clip_sound_player = ClipSoundPlayer()
        self._autostart_manager = AutostartManager()
        self._tray: ClipperTray | None = None
        self._engine_client: EngineClient | None = None
        self._engine_socket_path = ""
        self._engine_manager = EngineProcessManager(log_callback=self._log)
        self._hotkey_manager: GlobalHotkeyManager | None = None
        self._pending_hotkey_change: dict[str, Any] | None = None
        self._hotkey_capture_active = False
        self._accept_current_hotkey_capture = None
        self._hotkey_capture_release_id: int | None = None
        self._status_poll_id: int | None = None
        self._window_engine_status = _ENGINE_STATUS_READY
        self._display_capture_sync_pending = False
        self._engine_shutdown_pending = False
        self._idle_engine_exit_expected = False
        self._last_engine_exit_status: str | None = None
        self._capabilities_probe_requested = False
        self._capabilities_probe_count = 0
        self._pending_capture_mode_after_shutdown: str | None = None
        self._last_running_entry_key: str | None = None
        self._monitor_ipc: MonitorIpcServer | None = None
        self._monitor_socket_path = default_monitor_socket_path()
        self._monitor_manager = HostMonitorManager(
            config_file=self._config.path,
            socket_path=self._monitor_socket_path,
        )
        self._monitor_active_rules: dict[str, dict[str, Any]] = {}
        self._obs_studio_running = False
        self._obs_conflict_dialog_shown = False
        self._obs_conflict_notification_sent = False
        self._startup_diagnostics_logged = False
        self._display_diagnostics_logged = False
        self._display_target_request_pending = False
        self._display_target_request_callback = None
        self._display_target_request_poll_count = 0
        self._display_target_request_temporary_engine = False
        self._audio_learning_in_flight = False
        self._audio_learning_session_key: str | None = None
        self._audio_learning_candidate = ""
        self._audio_learning_candidate_count = 0
        self._audio_learning_last_diagnostic = ""
        self._audio_learning_resolved = False
        self._export_probe_start_id: int | None = None
        self._presentation_action: Gio.SimpleAction | None = None
        self._presentation_marks = {"window": 0, "clips": 0}
        self._fruit_drop_window = None

    def do_activate(self):
        """Called when the application is activated"""
        register_app_icon()
        if self._start_hidden:
            self._start_hidden = False
            if not self._background_hold:
                self.hold()
                self._background_hold = True
            self._refresh_tray_menu()
            return

        editor_window = getattr(self, "editor_window", None)
        if editor_window is not None:
            editor_window.present()
            self._release_background_hold()
            self._refresh_tray_menu()
            return

        if not self._config.get("setup_completed", False):
            self._present_setup_window()
            self._release_background_hold()
            return

        if not self.window:
            global MainWindow
            if MainWindow is None:
                from main_window import MainWindow

            self.window = MainWindow(
                application=self,
                config=self._config,
                engine_client=self._engine_client,
                hotkey_changed_callback=self._on_save_hotkey_changed,
                hotkey_capture_state_callback=self._set_hotkey_capture_active,
                capabilities_requested_callback=self.ensure_engine_for_capabilities,
                capabilities_finished_callback=self.on_capabilities_finished,
                engine_restart_required_callback=self.engine_restart_required_for_settings,
                engine_restart_requested_callback=lambda: self.on_restart_engine(None, None),
                start_on_boot_changed_callback=self.set_start_on_boot,
                display_target_change_callback=self.change_capture_target_display,
                show_display_target_controls=self._show_display_target_controls(),
                tray_available_callback=self._tray_available,
                capabilities_cache=self._capabilities_cache,
                edit_clip_callback=self.open_editor,
                presentation_callback=self._schedule_presentation_mark,
                language_changed_callback=self.request_language_change,
            )
            self.window.set_engine_status(self._window_engine_status)
            self.window.connect("notify::visible", self._on_window_visibility_changed)
            self.window.connect("close-request", self._on_window_close_request)
            # Arm this before present() so the benchmark observes GTK's earliest
            # submitted window frame rather than a later clip-list update.
            self._schedule_presentation_mark("window")
        self._log_display_diagnostics()
        self.window.present()
        updater = getattr(self, "_updater", None)
        if updater:
            updater.refresh_banner()
        if getattr(self, "_reopen_preferences", False):
            self._reopen_preferences = False
            GLib.idle_add(self.window.show_preferences)
        self._release_background_hold()
        if self._obs_studio_running and not self._obs_conflict_dialog_shown:
            self._show_obs_conflict_dialog()
        self._refresh_tray_menu()

    def do_startup(self):
        """Called when the application starts"""
        Adw.Application.do_startup(self)
        # GTK 4.22+ always registers through the desktop portal; older GTK
        # releases still require this property to receive ::query-end.
        self.props.register_session = True
        self.connect("query-end", self._on_session_query_end)
        self._log_startup_diagnostics()

        presentation_action = Gio.SimpleAction.new_stateful(
            _PRESENTATION_ACTION,
            None,
            GLib.Variant("s", self._presentation_state()),
        )
        presentation_action.set_enabled(False)
        self.add_action(presentation_action)
        self._presentation_action = presentation_action

        # Importing and running the FFmpeg export probe competes with the first
        # window and clip-list I/O. Warm it shortly after launch instead.
        self._export_probe_start_id = GLib.timeout_add_seconds(
            _EXPORT_PROBE_DELAY_SECONDS,
            self._start_export_capability_probe,
        )

        # Set up application-wide actions
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.request_quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_about)
        self.add_action(about_action)

        clear_token_action = Gio.SimpleAction.new("clear-pipewire-restore-token", None)
        clear_token_action.connect("activate", self.on_clear_pipewire_restore_token)
        self.add_action(clear_token_action)

        reset_config_action = Gio.SimpleAction.new("reset-config", None)
        reset_config_action.connect("activate", self.on_reset_config)
        self.add_action(reset_config_action)

        reset_factory_action = Gio.SimpleAction.new("reset-factory-settings", None)
        reset_factory_action.connect("activate", self.on_reset_factory_settings)
        self.add_action(reset_factory_action)

        restart_engine_action = Gio.SimpleAction.new("restart-engine", None)
        restart_engine_action.connect("activate", self.on_restart_engine)
        self.add_action(restart_engine_action)

        show_logs_action = Gio.SimpleAction.new("show-logs", None)
        show_logs_action.connect("activate", self.on_show_logs)
        self.add_action(show_logs_action)

        # Register the tray before slower monitor and portal setup. During an
        # idle GUI-to-background handoff this keeps the icon gap as short as
        # possible and prevents the host from showing the application fallback.
        self._tray = ClipperTray(
            show_callback=self._on_tray_visibility_toggle,
            save_clip_callback=self._on_tray_save_clip,
            quit_callback=self._on_tray_quit,
            window_visible_callback=self._is_window_visible,
        )

        # Set up the IPC client. The engine itself is started on demand by the
        # lightweight whitelist watcher in _poll_engine_status().
        self._setup_engine_client()
        if self._monitor_manager.available():
            self._setup_monitor_ipc()

        # Global save hotkey
        self._setup_global_hotkey()

        # Start polling engine status to update tray icon
        self._start_status_polling()
        self._migrate_background_autostart_command()
        from update_controller import UpdateController

        self._updater = UpdateController(self)

    def _presentation_state(self) -> str:
        """Return compact process-local timestamps for external diagnostics."""
        marks = getattr(self, "_presentation_marks", {"window": 0, "clips": 0})
        return json.dumps(
            {
                "pid": os.getpid(),
                "window": marks["window"],
                "clips": marks["clips"],
            },
            separators=(",", ":"),
        )

    def _start_export_capability_probe(self) -> bool:
        """Warm export capabilities after launch-critical UI work has settled."""
        self._export_probe_start_id = None
        from editor_export import start_export_encoder_capability_probe

        start_export_encoder_capability_probe()
        return False

    def _record_presentation_mark(
        self, content: str, presented_ns: int | None = None
    ) -> None:
        """Publish the first completed frame containing the requested content."""
        marks = getattr(self, "_presentation_marks", None)
        if marks is None:
            marks = {"window": 0, "clips": 0}
            self._presentation_marks = marks
        if content not in marks:
            return
        if marks[content]:
            return
        marks[content] = presented_ns or time.monotonic_ns()
        action = getattr(self, "_presentation_action", None)
        if action is not None:
            action.set_state(GLib.Variant("s", self._presentation_state()))

    def _schedule_presentation_mark(self, content: str) -> None:
        """Record content only after GTK finishes painting its next window frame."""
        if getattr(self, "_presentation_marks", {}).get(content):
            return
        window = self.window
        if window is None or not callable(getattr(window, "get_frame_clock", None)):
            return

        def arm_after_paint(*_args) -> None:
            frame_clock = window.get_frame_clock()
            if frame_clock is None:
                return
            handler = 0

            def on_after_paint(clock) -> None:
                clock.disconnect(handler)
                # This is GTK's frame submission boundary. Compositor-visible
                # presentation is intentionally outside the portable metric.
                frame_time_us = clock.get_frame_time()
                self._record_presentation_mark(
                    content,
                    frame_time_us * 1_000 if frame_time_us > 0 else None,
                )

            handler = frame_clock.connect("after-paint", on_after_paint)

        if window.get_realized():
            arm_after_paint()
        else:
            window.connect("realize", arm_after_paint)

    def do_shutdown(self):
        """Called when the application shuts down"""
        updater = getattr(self, "_updater", None)
        if updater:
            updater.close()
        if self._export_probe_start_id is not None:
            GLib.source_remove(self._export_probe_start_id)
            self._export_probe_start_id = None
        self._stop_status_polling()
        if self.setup_window:
            self.setup_window.cleanup()
        if self.window and hasattr(self.window, "cleanup"):
            self.window.cleanup()
        editor_window = getattr(self, "editor_window", None)
        if editor_window and hasattr(editor_window, "cleanup"):
            editor_window.cleanup()
        if self._engine_client:
            self._engine_client.set_auto_reconnect(False)
            self._engine_client.disconnect()
        self._monitor_manager.stop()
        if self._monitor_ipc:
            self._monitor_ipc.stop()
            self._monitor_ipc = None
        self._engine_manager.terminate()
        if self._hotkey_manager:
            self._hotkey_manager.stop()
        if self._tray:
            # The process exits or execs immediately after shutdown. Keep the
            # exported objects valid until D-Bus disconnects so the tray host
            # never observes a registered item with missing properties.
            self._tray.cleanup(preserve_registration=True)
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------
    # Tray callbacks
    # ------------------------------------------------------------------

    def _on_tray_visibility_toggle(self):
        """Show the main window, or hide it to keep Clipper in the tray."""
        window = getattr(self, "editor_window", None) or self.window
        if window and self._is_window_visible():
            self._hide_window_to_background(window)
            return

        self.do_activate()

    def _is_window_visible(self) -> bool:
        """Return whether either application window currently has visible GTK state."""
        editor_window = getattr(self, "editor_window", None)
        return bool(
            (editor_window and editor_window.get_visible())
            or (self.window and self.window.get_visible())
        )

    def _on_window_visibility_changed(self, *_args):
        """Refresh dynamic tray menu labels after the window is shown or hidden."""
        self._refresh_tray_menu()

    def _on_window_close_request(self, _window) -> bool:
        """Hide to tray on window close when configured and tray support exists."""
        window = self.window
        if window and self._should_minimize_to_tray_on_close():
            self._hide_window_to_background(window)
            return True
        if window:
            cleanup = getattr(window, "cleanup", None)
            if callable(cleanup):
                cleanup()
            self.window = None
        return False

    def _release_background_hold(self) -> None:
        """Let the presented application window own the foreground lifetime."""
        if not self._background_hold:
            return
        self.release()
        self._background_hold = False

    def _hide_window_to_background(self, window) -> None:
        """Release GTK/Vulkan state when hiding an engine-free window."""
        active_editor = getattr(self, "editor_window", None) is not None
        updating = getattr(getattr(self, "_updater", None), "busy", False)
        if active_editor or self._engine_manager.is_running() or updating:
            window.set_visible(False)
            if active_editor and not getattr(self, "_background_hold", False):
                self.hold()
                self._background_hold = True
            self._refresh_tray_menu()
            return

        self._restart_in_background = True
        self.quit()

    def _should_minimize_to_tray_on_close(self) -> bool:
        """Return whether the main window close button should hide to tray."""
        if not self._config.get("minimize_to_tray_on_close", False):
            return False
        return bool(self._tray and self._tray.is_available())

    def _refresh_tray_menu(self) -> None:
        if self._tray:
            self._tray.refresh_menu()

    def _tray_available(self) -> bool:
        return bool(self._tray and self._tray.is_available())

    def _on_tray_save_clip(self):
        """Save the current replay buffer."""
        if getattr(self, "_update_restart_pending", False):
            return
        if not self._engine_client or not self._engine_client.is_connected():
            self._show_error(_("Engine not running"))
            return

        entry = self._running_whitelist_entry(self._engine_manager.capture_mode)
        game_name = entry.get("name") if entry else None
        if not self._engine_client.save_replay_buffer(self._on_clip_saved, game_name=game_name):
            self._show_error(_("Could not send save command to Clipper engine"))

    def _on_tray_quit(self):
        """Quit the application from the tray."""
        # Quit is always final, even if it arrives while an idle window-to-
        # background handoff is pending.
        self._restart_in_background = False
        self.request_quit()

    def request_quit(self) -> None:
        """Route every interactive quit through the editor's dirty prompt."""
        updater = getattr(self, "_updater", None)
        if updater and updater.busy and not updater._checking:
            updater.show()
            return
        editor = getattr(self, "editor_window", None)
        if editor is not None:
            self._quit_after_editor = True
            editor.present()
            editor.request_close()
            return
        self.quit()

    def request_language_change(
        self, language: str, parent, *, reopen_preferences: bool = False
    ) -> None:
        """Save a language choice and offer a controlled application restart."""
        if self._config.get("ui_language", "system") == language:
            return
        self._config.set("ui_language", language)

        dialog = Adw.AlertDialog.new(
            _("Restart Clipper?"),
            _("Clipper needs to be restarted for this change to take effect."),
        )
        dialog.add_response("later", _("Restart later"))
        dialog.add_response("restart", _("Restart now"))
        dialog.set_default_response("restart")
        dialog.set_close_response("later")
        dialog.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_dialog, response: str) -> None:
            if response != "restart":
                return
            if reopen_preferences:
                os.environ[_REOPEN_PREFERENCES_ENV] = "1"
            self._restart_in_foreground = True
            self.quit()

        dialog.connect("response", on_response)
        dialog.present(parent)

    def _inhibit_logout_for_editor(self, editor) -> None:
        """Keep the session alive long enough to resolve an open video edit."""
        if getattr(self, "_editor_logout_inhibit_cookie", 0):
            return
        self._editor_logout_inhibit_cookie = self.inhibit(
            editor,
            Gtk.ApplicationInhibitFlags.LOGOUT,
            _("Save or discard the video edit before logging out"),
        )

    def _release_editor_logout_inhibitor(self) -> None:
        """Release the session inhibitor after the editor has been resolved."""
        cookie = getattr(self, "_editor_logout_inhibit_cookie", 0)
        if not cookie:
            return
        self._editor_logout_inhibit_cookie = 0
        self.uninhibit(cookie)

    def _on_session_query_end(self, *_args) -> None:
        """Show the editor's save prompt when the desktop is ending the session."""
        editor = getattr(self, "editor_window", None)
        if editor is None:
            self.quit()
            return
        if getattr(self, "_session_end_prompt_pending", False):
            editor.present()
            return

        self._session_end_prompt_pending = True
        self._quit_after_editor = True
        editor.present()
        editor.request_close(cancelled_callback=self._on_session_end_cancelled)

    def _on_session_end_cancelled(self) -> None:
        """Keep the edit open when its shutdown save prompt is cancelled."""
        self._session_end_prompt_pending = False
        self._quit_after_editor = False

    # ------------------------------------------------------------------
    # Global hotkey setup
    # ------------------------------------------------------------------

    def _setup_global_hotkey(self):
        """Register the configured global hotkey for saving clips."""
        self._hotkey_manager = GlobalHotkeyManager(
            self._on_global_save_hotkey,
            self._on_global_hotkey_error,
            self._on_global_hotkey_status,
        )
        if self._config.get("setup_completed", True):
            self._register_save_hotkey(self._configured_save_hotkey())

    def _on_save_hotkey_changed(self, hotkey: str) -> bool:
        """Request a new binding while retaining rollback state."""
        was_registered = bool(self._hotkey_manager and self._hotkey_manager.backend_name)
        if was_registered and hotkey == self._configured_save_hotkey():
            # The capture dialog already accepted the key. Keep the active
            # backend intact instead of reopening the desktop portal.
            return True
        self._pending_hotkey_change = {
            "save_hotkey": self._config.get("save_hotkey", "") if was_registered else "",
            "hotkey_binding_id": (
                self._config.get("hotkey_binding_id", "save-replay-buffer")
                if was_registered
                else "save-replay-buffer"
            ),
            "save_hotkey_portal_managed": (
                bool(self._config.get("save_hotkey_portal_managed", False))
                if was_registered
                else False
            ),
            "save_hotkey_portal_label": (
                self._config.get("save_hotkey_portal_label", "") if was_registered else ""
            ),
            "was_registered": was_registered,
            "requested_hotkey": hotkey,
            "requested_binding_id": shortcut_id_for_hotkey(hotkey),
        }
        started = self._register_save_hotkey(
            hotkey, self._pending_hotkey_change["requested_binding_id"]
        )
        if not started:
            self._rollback_pending_hotkey_change()
            return True
        pending = bool(self._hotkey_manager and self._hotkey_manager.registration_pending)
        if not pending:
            self._commit_requested_hotkey_change()
            self._pending_hotkey_change = None
        return pending

    def _register_save_hotkey(self, hotkey: str, shortcut_id: str | None = None) -> bool:
        if not self._hotkey_manager:
            return False

        if not shortcut_id:
            configured_shortcut_id = self._config.get("hotkey_binding_id", "save-replay-buffer")
            shortcut_id = (
                configured_shortcut_id
                if isinstance(configured_shortcut_id, str) and configured_shortcut_id
                else "save-replay-buffer"
            )
        if self._hotkey_manager.set_hotkey(hotkey, shortcut_id):
            if hotkey:
                action = (
                    "Requested save hotkey"
                    if self._hotkey_manager.registration_pending
                    else "Registered save hotkey"
                )
                self._log(
                    f"{action} {display_hotkey(hotkey)} via {self._hotkey_manager.backend_name}"
                )
                if self._hotkey_manager.backend_name != "XDG GlobalShortcuts portal":
                    self._set_portal_hotkey_state(False, None)
            else:
                self._log("Save hotkey disabled")
            return True

        error = self._hotkey_manager.last_error or "Unknown hotkey error"
        self._show_error(_("Could not register save hotkey: %(error)s") % {"error": error})
        return False

    def _on_global_save_hotkey(self):
        """Save the current replay buffer from the global hotkey."""
        if self._hotkey_capture_active:
            accept_current = getattr(self, "_accept_current_hotkey_capture", None)
            if callable(accept_current):
                accept_current()
            return
        self._on_tray_save_clip()

    def _set_hotkey_capture_active(self, active: bool, accept_current=None) -> None:
        """Suppress portal activations while Clipper is reading a shortcut."""
        if active:
            if self._hotkey_capture_release_id is not None:
                GLib.source_remove(self._hotkey_capture_release_id)
                self._hotkey_capture_release_id = None
            self._hotkey_capture_active = True
            self._accept_current_hotkey_capture = accept_current
            return

        self._accept_current_hotkey_capture = None
        if not self._hotkey_capture_active:
            return
        if self._hotkey_capture_release_id is not None:
            GLib.source_remove(self._hotkey_capture_release_id)
        self._hotkey_capture_release_id = GLib.timeout_add(
            _HOTKEY_CAPTURE_RELEASE_DELAY_MS,
            self._release_hotkey_capture_suppression,
        )

    def _release_hotkey_capture_suppression(self) -> bool:
        self._hotkey_capture_active = False
        self._hotkey_capture_release_id = None
        return False

    def _on_global_hotkey_error(self, message: str):
        """Report asynchronous global hotkey backend failures."""
        self._show_error(_("Global hotkey unavailable: %(message)s") % {"message": message})
        self._rollback_pending_hotkey_change()

    def _on_global_hotkey_status(self, status: str, trigger: str | None) -> None:
        """Log portal completion and mirror desktop-side shortcut edits."""
        self._commit_requested_hotkey_change()
        self._pending_hotkey_change = None
        if trigger:
            trigger = effective_hotkey_label(
                self._config.get("save_hotkey", ""),
                portal_managed=True,
                portal_label=trigger,
            )
        if status == "changed" and trigger:
            self._log(f"Desktop changed save hotkey to {trigger}")
        elif status == "changed":
            self._log("Desktop removed the save hotkey")
        elif trigger:
            self._log(f"Registered save hotkey {trigger} via XDG GlobalShortcuts portal")
        else:
            self._log("No save hotkey is bound via XDG GlobalShortcuts portal")

        self._set_portal_hotkey_state(True, trigger)
        if self.window:
            settings_view = getattr(self.window, "settings_view", None)
            update = getattr(settings_view, "set_save_hotkey_from_portal", None)
            if callable(update):
                update(trigger)
        if self.setup_window:
            self.setup_window.set_save_hotkey_from_portal(trigger)

    def _commit_requested_hotkey_change(self) -> None:
        """Persist a requested shortcut after its backend accepts it."""
        pending = self._pending_hotkey_change
        if not pending:
            return
        requested_hotkey = pending["requested_hotkey"]
        requested_binding_id = pending["requested_binding_id"]
        if self._config.get("save_hotkey", "") != requested_hotkey:
            self._config.set("save_hotkey", requested_hotkey)
        if self._config.get("hotkey_binding_id") != requested_binding_id:
            self._config.set("hotkey_binding_id", requested_binding_id)
        self._set_portal_hotkey_state(False, None)

    def _rollback_pending_hotkey_change(self) -> None:
        """Reconcile configuration after a rejected binding request."""
        previous = self._pending_hotkey_change
        if not previous:
            return
        self._pending_hotkey_change = None
        for key in (
            "save_hotkey",
            "hotkey_binding_id",
            "save_hotkey_portal_label",
            "save_hotkey_portal_managed",
        ):
            if self._config.get(key) != previous[key]:
                self._config.set(key, previous[key])

        # KDE registers a rejected replacement as an unbound action and
        # removes the previously approved action for the same application.
        # Re-requesting the old action opens another portal dialog and still
        # cannot restore it without user approval, so report the actual
        # effective state instead.
        if (
            previous["was_registered"]
            and self._hotkey_manager
            and self._hotkey_manager.backend_name == "XDG GlobalShortcuts portal"
        ):
            self._set_portal_hotkey_state(True, None)
        self._refresh_hotkey_ui_from_config()

    def _refresh_hotkey_ui_from_config(self) -> None:
        if self.window:
            settings_view = getattr(self.window, "settings_view", None)
            refresh = getattr(settings_view, "refresh_save_hotkey_from_config", None)
            if callable(refresh):
                refresh()
        if self.setup_window:
            self.setup_window.refresh_save_hotkey_from_config()

    def _set_portal_hotkey_state(self, managed: bool, trigger: str | None) -> None:
        """Persist the portal's display-only effective shortcut state."""
        label = trigger.strip() if isinstance(trigger, str) else ""
        if self._config.get("save_hotkey_portal_label", "") != label:
            self._config.set("save_hotkey_portal_label", label)
        if bool(self._config.get("save_hotkey_portal_managed", False)) != managed:
            self._config.set("save_hotkey_portal_managed", managed)

    # ------------------------------------------------------------------
    # Engine client setup
    # ------------------------------------------------------------------

    def _setup_engine_client(self):
        """Initialize the engine IPC client."""
        # Determine socket path
        uid = os.getuid()
        run_dir = f"/run/user/{uid}"
        if os.path.isdir(run_dir):
            socket_path = f"{run_dir}/clipper-engine.sock"
        else:
            socket_path = f"/tmp/clipper-engine-{uid}.sock"

        self._engine_client = EngineClient(socket_path)
        self._engine_client.on_error(self._on_engine_error)
        self._engine_client.on("display_target_cancelled", self._on_display_target_cancelled)
        self._engine_socket_path = socket_path

    def _setup_monitor_ipc(self) -> None:
        """Start automatic host-process detection for the Flatpak app."""
        self._monitor_ipc = MonitorIpcServer(
            self._on_monitor_event,
            socket_path=self._monitor_socket_path,
        )
        if not self._monitor_ipc.start():
            self._monitor_ipc = None
            self._log("[clipper] WARN: could not start host monitor IPC server")
            return
        if not self._monitor_manager.start():
            self._log("[clipper] WARN: could not start automatic process detection")

    def _on_monitor_event(self, event: dict) -> bool:
        """Handle a validated event from clipper-monitor-host."""
        rule_id = str(event.get("rule_id") or "")
        if not rule_id:
            return False

        if rule_id == _OBS_STUDIO_MONITOR_RULE_ID:
            self._set_obs_studio_running(event.get("event") == "process_started")
            return False

        if event.get("event") == "process_started":
            self._monitor_active_rules[rule_id] = event
            entry = self._entry_for_monitor_rule_id(rule_id)
            if entry is not None:
                self._log_running_entry(entry)
        elif event.get("event") == "process_stopped":
            entry = self._entry_for_monitor_rule_id(rule_id)
            self._monitor_active_rules.pop(rule_id, None)
            if not self._monitor_active_rules:
                if entry is not None:
                    name = entry.get("name", "Unknown game")
                    self._log(f"[clipper] {name} is no longer running, stopping capture")
                self._last_running_entry_key = None

        self._poll_engine_status()
        return False

    def _set_obs_studio_running(self, running: bool) -> None:
        """Update conflict state and warn once for each concurrent run."""
        running = bool(running)
        if running == self._obs_studio_running:
            return

        self._obs_studio_running = running
        if not running:
            self._obs_conflict_dialog_shown = False
            self._obs_conflict_notification_sent = False
            withdraw = getattr(self, "withdraw_notification", None)
            if callable(withdraw):
                withdraw(_OBS_CONFLICT_NOTIFICATION_ID)
            return

        self._log(f"[clipper] WARNING: {_OBS_CONFLICT_TITLE}")
        if self._is_window_visible():
            self._show_obs_conflict_dialog()
        else:
            self._send_obs_conflict_notification()

    def _send_obs_conflict_notification(self) -> None:
        if self._obs_conflict_notification_sent:
            return

        notification = Gio.Notification.new(_OBS_CONFLICT_TITLE)
        notification.set_body(_OBS_CONFLICT_BODY)
        self.send_notification(_OBS_CONFLICT_NOTIFICATION_ID, notification)
        self._obs_conflict_notification_sent = True

    def _show_obs_conflict_dialog(self) -> None:
        window = self.window
        if window is None or self._obs_conflict_dialog_shown:
            return

        dialog = Adw.AlertDialog.new(_OBS_CONFLICT_TITLE, _OBS_CONFLICT_BODY)
        dialog.add_response("acknowledge", _("Acknowledge"))
        dialog.set_response_appearance("acknowledge", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("acknowledge")
        dialog.set_default_response("acknowledge")
        dialog.present(window)
        self._obs_conflict_dialog_shown = True

    def _poll_local_obs_studio(self) -> None:
        """Detect OBS outside Flatpak, or if the host monitor stopped."""
        if self._monitor_manager.available() and self._monitor_manager.is_running():
            return

        self._set_obs_studio_running(find_running_obs_studio() is not None)

    def _on_engine_error(self, message: str):
        """Handle engine connection/communication errors."""
        is_disconnect = any(
            marker in message.lower()
            for marker in (
                "disconnected",
                "closed connection",
                "socket read error",
                "broken pipe",
            )
        )
        if is_disconnect:
            expected_shutdown = self._engine_shutdown_pending
            self._engine_shutdown_pending = False
            self._display_capture_sync_pending = False
            engine_exit_code = self._engine_manager.poll()
            if engine_exit_code is not None:
                self._handle_engine_process_exit(engine_exit_code)
            self._start_pending_capture_after_shutdown()
            if expected_shutdown:
                return

        self._log(f"Engine error: {message}")

    def _restart_engine(self, reason: str | None = None) -> None:
        if not self._engine_client:
            return
        reason = reason or _("Restarting engine…")

        if self._engine_client.is_connected():
            self._engine_client.set_auto_reconnect(True)
            if self._engine_client.shutdown(
                lambda response: self._on_engine_restart_response(response, reason),
                restart=True,
            ):
                return

            self._show_error(_("Could not send restart command to Clipper engine"))
            return

        running_entry = self._running_whitelist_entry()
        if running_entry is None:
            self._show_toast(_("Engine will start when a whitelisted game is running"))
            return

        if self._start_engine_for_capture(capture_mode_for_entry(running_entry)):
            self._show_toast(reason)

    def _on_engine_restart_response(self, response: dict, reason: str) -> None:
        if response.get("ok"):
            self._show_toast(reason)
        else:
            error = response.get("error", "Unknown error")
            self._show_error(_("Engine restart failed: %(error)s") % {"error": error})

    def ensure_engine_for_capabilities(self) -> bool:
        """Temporarily start the engine so Settings can query OBS capabilities."""
        temporary_probe = self._running_whitelist_entry() is None
        if temporary_probe:
            if not self._capabilities_probe_requested:
                self._log("[clipper] OBS settings probe requested (temporary engine)")
            self._capabilities_probe_count += 1
            self._capabilities_probe_requested = True

        if self._engine_manager.is_running():
            self._connect_to_engine_if_available()
            return True

        if self._start_engine_for_capture(CAPTURE_MODE_GAME):
            return True

        if temporary_probe:
            self._finish_capabilities_probe_unit()
        return False

    def on_capabilities_finished(self) -> None:
        """Stop a temporary capability probe once Settings has the response."""
        self._finish_capabilities_probe_unit(shutdown_when_idle=True)

    def _finish_capabilities_probe_unit(self, *, shutdown_when_idle: bool = False) -> None:
        if not self._capabilities_probe_requested:
            return

        if self._capabilities_probe_count > 0:
            self._capabilities_probe_count -= 1

        if self._capabilities_probe_count > 0:
            return

        self._capabilities_probe_requested = False
        self._log("[clipper] OBS settings probe finished")
        if (
            shutdown_when_idle
            and self._running_whitelist_entry() is None
            and not self._display_target_request_pending
        ):
            self._shutdown_idle_engine()

    def engine_restart_required_for_settings(self) -> bool:
        """Return whether settings changes need a live engine restart."""
        return self._engine_manager.is_running() and self._running_whitelist_entry() is not None

    def _show_toast(self, message: str) -> None:
        self._log(message)
        if self.window and hasattr(self.window, "show_toast"):
            self.window.show_toast(message)

    def set_start_on_boot(self, enabled: bool, callback) -> None:
        """Apply automatic login startup through the platform integration."""
        self._autostart_manager.set_enabled(enabled, callback)

    def _present_setup_window(self) -> None:
        """Present the full first-run setup flow instead of the main window."""
        if self.setup_window is None:
            global SetupWindow
            if SetupWindow is None:
                from setup_window import SetupWindow

            self.setup_window = SetupWindow(
                application=self,
                config=self._config,
                engine_client=self._engine_client,
                capabilities_requested_callback=self.ensure_engine_for_capabilities,
                capabilities_finished_callback=self.on_capabilities_finished,
                hotkey_changed_callback=self._on_save_hotkey_changed,
                hotkey_capture_state_callback=self._set_hotkey_capture_active,
                display_target_callback=self.change_capture_target_display,
                show_display_target_controls=self._show_display_target_controls(),
                completed_callback=self._on_setup_completed,
                language_changed_callback=lambda language, parent: self.request_language_change(
                    language, parent, reopen_preferences=False
                ),
            )
            self.setup_window.connect("close-request", self._on_setup_window_closed)
        self.setup_window.present()

    def _show_display_target_controls(self) -> bool:
        """Return whether the session supports portal display selection UI."""
        return not getattr(self, "_is_x11", _is_x11_session())

    def _on_setup_window_closed(self, setup_window) -> bool:
        if setup_window is not self.setup_window:
            return False

        self.setup_window = None
        if not self._config.get("setup_completed", False):
            # An unfinished setup window is the application's only usable UI.
            # Closing it must not leave a hidden, tray-only process behind.
            self.quit()
        return False

    def _on_setup_completed(self) -> None:
        """Close setup, activate the shortcut, and enter the main application."""
        setup_window = self.setup_window
        self.setup_window = None
        if setup_window is not None:
            setup_window.close()
        hotkey = self._configured_save_hotkey()
        if hotkey and not (self._hotkey_manager and self._hotkey_manager.backend_name):
            self._on_save_hotkey_changed(hotkey)
        self._refresh_window_from_config()
        if self.window is not None:
            self.window.present()
        else:
            self.do_activate()
        self._poll_engine_status()

    def change_capture_target_display(self, completion_callback=None) -> bool:
        """Open the system display picker now and persist the selected display."""
        if not self._show_display_target_controls():
            message = _(
                "X11 capture uses the primary X11 display without a system picker."
            )
            if completion_callback:
                completion_callback(False, message)
            else:
                self._show_toast(message)
            return False

        if self._display_target_request_pending:
            message = _("The system display picker is already open.")
            if completion_callback:
                completion_callback(False, message)
            else:
                self._show_toast(message)
            return False

        running_entry = self._running_whitelist_entry()
        if running_entry is not None and self._engine_manager.capture_mode != CAPTURE_MODE_DISPLAY:
            message = _(
                "Stop the active game capture before changing the target display."
            )
            if completion_callback:
                completion_callback(False, message)
            else:
                self._show_toast(message)
            return False

        self._config.clear_pipewire_restore_token()
        self._config.set("display_capture_guidance_seen", True)
        self._display_target_request_pending = True
        self._display_target_request_callback = completion_callback
        self._display_target_request_poll_count = 0
        self._display_target_request_temporary_engine = running_entry is None

        if self._engine_client and self._engine_client.is_connected():
            self._engine_client.set_auto_reconnect(False)
            self._engine_client.disconnect(auto_reconnect=False)
        if self._engine_manager.is_running():
            self._engine_manager.terminate()

        if self._start_engine_for_capture(CAPTURE_MODE_DISPLAY):
            self._log("[clipper] Opened system picker for target display")
            return True

        self._finish_display_target_request(
            False, "Could not start display capture for the system picker."
        )
        return False

    def _migrate_background_autostart_command(self) -> None:
        """Replace legacy foreground login entries once per configuration."""
        if not self._config.get("start_on_boot", False):
            return
        if self._config.get("autostart_background_mode_configured", False):
            return

        # The XDG autostart generator may already have cached the legacy
        # foreground command for this login. Hide this one activation as well
        # as rewriting the portal entry, so upgrades do not require two logins
        # before startup becomes silent.
        self._start_hidden = True

        def on_migrated(success: bool, error: str | None = None) -> None:
            if success:
                self._config.set("autostart_background_mode_configured", True)
                return
            self._log(
                f"clipper: could not migrate start-on-boot command: {error or 'unknown error'}"
            )

        self._autostart_manager.set_enabled(True, on_migrated)

    def _disable_autostart_after_reset(self) -> None:
        """Keep desktop autostart registration aligned with reset defaults."""

        def on_disabled(success: bool, error: str | None = None) -> None:
            if not success:
                self._show_error(error or _("Could not disable automatic startup"))

        self._autostart_manager.set_enabled(False, on_disabled)

    def on_clear_pipewire_restore_token(self, action, param):
        """Clear the saved PipeWire portal restore token."""
        self._config.clear_pipewire_restore_token()
        self._show_toast(_("PipeWire restore token cleared"))

    def on_show_logs(self, _action, _param) -> None:
        """Open the live application and engine log viewer."""
        log_window_ref = getattr(self, "_log_window_ref", None)
        log_window = log_window_ref() if log_window_ref is not None else None
        if log_window is None or getattr(log_window, "_destroyed", False):
            log_window = LogWindow(self._log_buffer, transient_for=self.window)
            self._log_window_ref = weakref.ref(log_window)
        log_window.present()

    def on_reset_config(self, action, param):
        """Reset user-facing config to defaults and reopen setup."""
        self._config.reset_to_defaults()
        self._disable_autostart_after_reset()
        self._begin_setup_after_reset()

    def on_reset_factory_settings(self, action, param):
        """Reset all config and engine-managed persisted state."""
        self._config.reset_to_factory_settings()
        self._disable_autostart_after_reset()
        self._begin_setup_after_reset()

    def _begin_setup_after_reset(self) -> None:
        """Stop active services and return a reset installation to setup."""
        self._register_save_hotkey("")
        if self._engine_client and self._engine_client.is_connected():
            self._engine_client.set_auto_reconnect(False)
            self._engine_client.disconnect(auto_reconnect=False)
        self._engine_manager.terminate()
        self._refresh_window_from_config()
        if self.window is not None:
            self.window.set_visible(False)
        self._present_setup_window()

    def _refresh_window_from_config(self) -> None:
        """Apply an externally replaced configuration to the open window."""
        if self.window and hasattr(self.window, "reload_from_config"):
            self.window.reload_from_config()

    def on_restart_engine(self, action, param):
        """Restart the engine without changing config."""
        self._restart_engine()

    def _on_clip_saved(self, response: dict):
        """Handle completion of a replay-buffer save."""
        if response.get("ok"):
            self._log("Clip saved successfully")
            # The engine completion event is the authoritative signal that the
            # file is ready.  Refresh directly instead of relying only on a
            # directory-monitor event, which can be missed by sandboxed mounts.
            window = getattr(self, "window", None)
            clips_view = getattr(window, "clips_view", None)
            refresh = getattr(clips_view, "refresh", None)
            if callable(refresh):
                refresh()
            if self._config.get("play_sound_on_clip_saved", False):
                player = getattr(self, "_clip_sound_player", None)
                if player is None:
                    player = ClipSoundPlayer()
                    self._clip_sound_player = player
                volume = normalize_clip_sound_volume(
                    self._config.get("clip_sound_volume", 1.0)
                )
                if not player.play(volume):
                    self._log("Could not play clip saved sound")
            if self._config.get("notify_on_clip_saved", True):
                notification = Gio.Notification.new(_("Clip captured"))
                notification.set_body(_("Your clip has been saved."))
                self.send_notification("clip-captured", notification)
        else:
            error = response.get("error", "Unknown error")
            if error == "save_failed":
                error = _(
                    "Could not write the clip. Check the save folder and available disk space."
                )
            self._show_error(_("Failed to save clip: %(error)s") % {"error": error})

    def open_editor(self, clip_path, completed_callback=None) -> None:
        """Probe a clip off the GTK thread and present its saved or new edit."""
        if getattr(self, "_update_restart_pending", False):
            if completed_callback:
                completed_callback()
            return
        if self.editor_window is not None:
            self.editor_window.present()
            if completed_callback:
                completed_callback()
            return
        if getattr(self, "_editor_open_pending", False):
            if completed_callback:
                completed_callback()
            return
        self._editor_open_pending = True

        def worker():
            try:
                from editor_drafts import load_or_create_history
                from editor_media import probe_media

                history = load_or_create_history(probe_media(clip_path))
                GLib.idle_add(finished, history, None)
            except Exception as error:
                GLib.idle_add(finished, None, error)

        def finished(history, error):
            self._editor_open_pending = False
            if completed_callback:
                completed_callback()
            if error is not None:
                self._show_error(
                    _("Could not open clip editor: %(error)s") % {"error": error}
                )
                return False
            if self.editor_window is not None:
                return False
            from editor_window import EditorWindow

            project = history.project
            self.editor_window = EditorWindow(
                application=self,
                project=project,
                config=self._config,
                finished_callback=self._on_editor_finished,
                history=history,
            )
            self._inhibit_logout_for_editor(self.editor_window)
            self.editor_window.connect(
                "notify::visible", self._on_window_visibility_changed
            )
            if self.window:
                self.window.set_visible(False)
            self.editor_window.present()
            self._refresh_tray_menu()
            return False

        threading.Thread(target=worker, daemon=True).start()

    def _on_editor_finished(self, editor_window) -> None:
        """Return from the editor to the existing Clips window."""
        if editor_window is not self.editor_window:
            return
        self.editor_window = None
        editor_window.destroy()
        self._session_end_prompt_pending = False
        self._release_editor_logout_inhibitor()
        if getattr(self, "_quit_after_editor", False):
            self._quit_after_editor = False
            self.quit()
            return
        if self.window is not None:
            clips_view = getattr(self.window, "clips_view", None)
            if clips_view is not None:
                clips_view.refresh()
            view_stack = getattr(self.window, "view_stack", None)
            if view_stack is not None:
                view_stack.set_visible_child_name("clips")
            self.window.present()
        else:
            self.do_activate()
        self._release_background_hold()
        self._refresh_tray_menu()

    def _show_error(self, message: str):
        """Show an error message to the user."""
        self._log(f"Error: {message}")
        if self.window and hasattr(self.window, "show_toast"):
            self.window.show_toast(message)

    def _log(self, message: str) -> None:
        """Record a message for the debug log viewer and standard output."""
        log_buffer = getattr(self, "_log_buffer", None)
        if log_buffer is not None:
            log_buffer.add(message)
        print(message)

    def _log_startup_diagnostics(self) -> None:
        """Log a compact, privacy-conscious summary of the runtime session."""
        if getattr(self, "_startup_diagnostics_logged", False):
            return
        self._startup_diagnostics_logged = True
        session = os.environ.get("XDG_SESSION_TYPE") or (
            "wayland"
            if os.environ.get("WAYLAND_DISPLAY")
            else "x11"
            if os.environ.get("DISPLAY")
            else "unknown"
        )
        desktop = (
            os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"
        )
        sandbox = (
            "Flatpak"
            if (os.environ.get("FLATPAK_ID") or os.path.exists("/.flatpak-info"))
            else "native"
        )
        cpu_count = os.cpu_count()
        cpu_summary = f"{cpu_count} logical CPUs" if cpu_count else "CPU count unknown"
        self._log(f"[clipper] Session: {session}; desktop: {desktop}; runtime: {sandbox}")
        self._log(
            f"[clipper] System: {platform.machine() or 'unknown architecture'}; {cpu_summary}"
        )

        load_status = getattr(self._config, "load_status", "unknown")
        if load_status == "loaded":
            key_count = getattr(self._config, "saved_key_count", 0)
            self._log(f"[clipper] Local config: loaded existing config ({key_count} saved keys)")
        elif load_status == "defaults_missing":
            self._log("[clipper] Local config: no existing config; using built-in defaults")
        elif load_status == "defaults_invalid":
            self._log("[clipper] Local config: invalid or unreadable; using built-in defaults")
        else:
            self._log("[clipper] Local config: status unavailable")

    def _log_display_diagnostics(self) -> None:
        """Log monitor geometry once GTK has an active display."""
        if getattr(self, "_display_diagnostics_logged", False) or not self.window:
            return
        get_display = getattr(self.window, "get_display", None)
        if not callable(get_display):
            return
        display = get_display()
        get_monitors = getattr(display, "get_monitors", None) if display else None
        if not callable(get_monitors):
            return

        monitors = get_monitors()
        count = monitors.get_n_items()
        summaries = []
        for index in range(count):
            monitor = monitors.get_item(index)
            if monitor is None:
                continue
            geometry = monitor.get_geometry()
            width = geometry.width
            height = geometry.height
            refresh_mhz = monitor.get_refresh_rate()
            refresh = f" @ {refresh_mhz / 1000:g} Hz" if refresh_mhz > 0 else ""
            connector = monitor.get_connector() or f"monitor {index + 1}"
            get_scale = getattr(monitor, "get_scale", None)
            scale = cast(float, get_scale() if callable(get_scale) else monitor.get_scale_factor())
            portrait_aspect = height / width if width > 0 and height > width else 0.0
            rotated_scale_artifact = (
                scale > 1 and portrait_aspect > 0 and abs(scale - portrait_aspect) < 0.01
            )
            summary = f"{connector} {width}x{height} logical{refresh}"
            if rotated_scale_artifact:
                # GDK 4 can derive a bogus fractional/integer scale from the
                # unrotated mode dimensions (for example, 1920 / 1080 = 1.78,
                # rounded to a 2x buffer scale). Do not present that as the
                # desktop's configured output scale.
                summary += ", rotated; scale unavailable (GTK rotation artifact)"
            else:
                summary += f", scale {scale:g}x"
                if scale > 1:
                    summary += f" (~{width * scale:g}x{height * scale:g} pixels)"
            summaries.append(summary)

        if summaries:
            noun = "monitor" if len(summaries) == 1 else "monitors"
            self._log(f"[clipper] Displays: {len(summaries)} {noun}; " + "; ".join(summaries))
            self._display_diagnostics_logged = True

    # ------------------------------------------------------------------
    # Status polling for tray icon
    # ------------------------------------------------------------------

    def _start_status_polling(self):
        """Start periodic polling of engine status to update tray icon."""
        if self._status_poll_id is None:
            # Poll every 2 seconds
            self._status_poll_id = GLib.timeout_add_seconds(2, self._poll_engine_status)
            # Do an immediate update as well
            self._poll_engine_status()

    def _stop_status_polling(self):
        """Stop polling engine status."""
        if self._status_poll_id is not None:
            GLib.source_remove(self._status_poll_id)
            self._status_poll_id = None

    def _poll_engine_status(self) -> bool:
        """Poll engine status and request an update. Returns True to continue polling."""
        self._poll_local_obs_studio()
        engine_exit_code = self._engine_manager.poll()
        if engine_exit_code is not None:
            self._handle_engine_process_exit(engine_exit_code)

        running_entry = self._running_whitelist_entry()

        if running_entry is not None:
            self._capabilities_probe_requested = False
            self._capabilities_probe_count = 0
            self._log_running_entry(running_entry)
            capture_mode = capture_mode_for_entry(running_entry)
            if self._engine_shutdown_pending:
                self._pending_capture_mode_after_shutdown = capture_mode
                return True
            self._start_engine_for_capture(capture_mode)
        elif self._capabilities_probe_requested or self._display_target_request_pending:
            self._reset_game_audio_learning()
            self._connect_to_engine_if_available()
        else:
            self._reset_game_audio_learning()
            if self._shutdown_idle_engine():
                if self._tray:
                    self._tray.set_status(ClipperTray.STATUS_IDLE)
                self._set_window_engine_status(self._idle_window_engine_status())
                return True

        self._connect_to_engine_if_available()

        if not self._engine_client or not self._engine_client.is_connected():
            # Engine not available - set tray to idle
            if self._tray:
                self._tray.set_status(ClipperTray.STATUS_IDLE)
            self._set_window_engine_status(self._idle_window_engine_status())
            return True  # Continue polling

        # Request status from engine
        self._engine_client.get_status(self._on_status_response)
        return True  # Continue polling

    def _handle_engine_process_exit(self, exit_code: int) -> None:
        if self._display_target_request_pending:
            self._finish_display_target_request(
                False, "The display picker closed before a target was saved."
            )
        if self._idle_engine_exit_expected:
            self._idle_engine_exit_expected = False
            if exit_code == 0:
                self._last_engine_exit_status = None
                self._set_window_engine_status(_ENGINE_STATUS_READY)
                return

        if exit_code == RESTART_EXIT_CODE or self._engine_shutdown_pending:
            return

        if exit_code == 0:
            self._last_engine_exit_status = _ENGINE_STATUS_EXITED
        else:
            self._last_engine_exit_status = _ENGINE_STATUS_CRASHED
        self._set_window_engine_status(self._last_engine_exit_status)

    def _idle_window_engine_status(self) -> str:
        return self._last_engine_exit_status or _ENGINE_STATUS_READY

    def _running_whitelist_entry(self, capture_mode: str | None = None) -> dict | None:
        whitelist = self._configured_whitelist()
        entry = self._running_monitor_whitelist_entry(whitelist, capture_mode)
        if entry is not None:
            return entry
        return find_running_whitelist_entry(whitelist, capture_mode=capture_mode)

    @staticmethod
    def _game_audio_learning_key(entry: dict) -> str:
        return str(
            entry.get("appid")
            or entry.get("install_path")
            or entry.get("path")
            or entry.get("name")
            or ""
        )

    def _reset_game_audio_learning(self) -> None:
        self._audio_learning_session_key = None
        self._audio_learning_candidate = ""
        self._audio_learning_candidate_count = 0
        self._audio_learning_last_diagnostic = ""
        self._audio_learning_resolved = False

    def _maybe_learn_game_audio_identity(self, entry: dict) -> None:
        """Probe the active game's real playback process without blocking GTK."""
        audio = self._config.get("audio", {})
        if not audio_uses_whitelisted_game(audio, entry):
            return

        key = self._game_audio_learning_key(entry)
        if not key:
            return
        if getattr(self, "_audio_learning_session_key", None) != key:
            self._audio_learning_session_key = key
            self._audio_learning_candidate = ""
            self._audio_learning_candidate_count = 0
            self._audio_learning_last_diagnostic = ""
            self._audio_learning_resolved = False
            name = str(entry.get("name") or "game")
            selector = str(entry.get("audio_process") or "not learned")
            self._log(
                f"[clipper] Audio learning started for {name}; configured selector: {selector}"
            )
        if getattr(self, "_audio_learning_resolved", False):
            return
        if getattr(self, "_audio_learning_in_flight", False):
            return
        if not callable(getattr(self._monitor_manager, "list_processes", None)):
            return

        whitelist = self._configured_whitelist()
        try:
            entry_index = next(
                index for index, candidate in enumerate(whitelist) if candidate is entry
            )
        except StopIteration:
            entry_index = next(
                (
                    index
                    for index, candidate in enumerate(whitelist)
                    if self._game_audio_learning_key(candidate) == key
                ),
                -1,
            )
        if entry_index < 0:
            return
        rule_id = monitor_rule_id(entry, entry_index)
        self._audio_learning_in_flight = True

        def probe() -> None:
            identity = ""
            diagnostics: dict[str, Any] = {}
            try:
                processes = self._monitor_manager.list_processes()
                if processes is not None:
                    identity, diagnostics = probe_game_audio_identity(
                        entry,
                        rule_id,
                        processes,
                        list_playback_audio_sources(),
                    )
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_game_audio_learning_error, key, str(exc))
                return
            GLib.idle_add(
                self._on_game_audio_learning_result,
                key,
                identity,
                diagnostics,
            )

        threading.Thread(
            target=probe,
            name="clipper-game-audio-learning",
            daemon=True,
        ).start()

    def _on_game_audio_learning_error(self, key: str, error: str) -> bool:
        self._audio_learning_in_flight = False
        if getattr(self, "_audio_learning_session_key", None) == key:
            self._log(f"[clipper] WARN: could not learn game audio application: {error}")
        return False

    def _on_game_audio_learning_result(
        self,
        key: str,
        identity: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> bool:
        self._audio_learning_in_flight = False
        running_entry = self._running_whitelist_entry()
        if (
            running_entry is None
            or self._game_audio_learning_key(running_entry) != key
            or getattr(self, "_audio_learning_session_key", None) != key
        ):
            return False
        diagnostic_text = (
            format_game_audio_probe(diagnostics) if isinstance(diagnostics, dict) else ""
        )
        if not identity:
            self._audio_learning_candidate = ""
            self._audio_learning_candidate_count = 0
            if diagnostic_text and diagnostic_text != getattr(
                self, "_audio_learning_last_diagnostic", ""
            ):
                name = str(running_entry.get("name") or "game")
                self._log(
                    f"[clipper] Audio probe for {name}: no verified stream; {diagnostic_text}"
                )
                self._audio_learning_last_diagnostic = diagnostic_text
            return False

        if identity.casefold() == getattr(self, "_audio_learning_candidate", "").casefold():
            self._audio_learning_candidate_count += 1
        else:
            self._audio_learning_candidate = identity
            self._audio_learning_candidate_count = 1
            method = str((diagnostics or {}).get("match_method") or "identity")
            pid = int((diagnostics or {}).get("match_pid") or 0)
            pid_text = f" PID {pid}" if pid > 0 else ""
            name = str(running_entry.get("name") or "game")
            self._log(
                f"[clipper] Audio probe for {name}: candidate {identity} "
                f"matched by {method}{pid_text}; confirming"
            )
        self._audio_learning_last_diagnostic = diagnostic_text

        # Requiring the same live playback identity in two consecutive probes
        # avoids learning a short-lived launcher or splash-screen stream.
        if self._audio_learning_candidate_count < 2:
            return False

        configured_identity = str(running_entry.get("audio_process") or "")
        if configured_identity.casefold() == identity.casefold():
            self._audio_learning_resolved = True
            return False

        whitelist, audio, changed = apply_learned_game_audio_identity(
            self._config.get("whitelist", []),
            self._config.get("audio", {}),
            running_entry,
            identity,
        )
        if not changed:
            self._audio_learning_resolved = True
            return False

        self._config.set("whitelist", whitelist)
        self._config.set("audio", audio)
        self._audio_learning_resolved = True
        name = str(running_entry.get("name") or "game")
        self._log(f"[clipper] Learned {identity} as the audio application for {name}")
        self._refresh_window_from_config()
        if self._engine_manager.is_running():
            self._restart_engine(f"Updated audio capture for {name}")
        return False

    def _running_monitor_whitelist_entry(
        self, whitelist: list[dict], capture_mode: str | None = None
    ) -> dict | None:
        if not self._monitor_active_rules:
            return None

        for idx, entry in enumerate(whitelist):
            if capture_mode is not None and capture_mode_for_entry(entry) != capture_mode:
                continue
            if monitor_rule_id(entry, idx) in self._monitor_active_rules:
                return entry
        return None

    def _entry_for_monitor_rule_id(self, rule_id: str) -> dict | None:
        for idx, entry in enumerate(self._configured_whitelist()):
            if monitor_rule_id(entry, idx) == rule_id:
                return entry
        return None

    def _log_running_entry(self, entry: dict) -> None:
        key = str(entry.get("appid") or entry.get("path") or entry.get("name") or "")
        if key and key == self._last_running_entry_key:
            return

        self._last_running_entry_key = key
        name = entry.get("name", "Unknown game")
        mode = capture_mode_for_entry(entry).replace("_", " ")
        self._log(f"[clipper] {name} detected, starting recording with {mode} mode")

    def _start_engine_for_capture(
        self, capture_mode: str = DEFAULT_CAPTURE_MODE, *, force_restart: bool = False
    ) -> bool:
        if self._engine_shutdown_pending:
            self._pending_capture_mode_after_shutdown = capture_mode
            return False

        try:
            if force_restart and self._engine_manager.is_running():
                self._engine_manager.terminate()

            if (
                self._engine_client
                and self._engine_client.is_connected()
                and self._engine_manager.is_running()
                and self._engine_manager.capture_mode != capture_mode
            ):
                self._engine_client.set_auto_reconnect(False)
                self._engine_client.disconnect(auto_reconnect=False)
                self._log(f"[clipper] Switching engine capture mode to {capture_mode}")
            elif not self._engine_manager.is_running():
                self._log(f"[clipper] Starting engine for {capture_mode}")
            started = self._engine_manager.start(capture_mode)
        except Exception as exc:  # noqa: BLE001
            self._show_error(_("Could not start Clipper engine: %(error)s") % {"error": exc})
            return False

        if started:
            self._last_engine_exit_status = None
        self._connect_to_engine_if_available()
        return started

    def _start_pending_capture_after_shutdown(self) -> bool:
        capture_mode = self._pending_capture_mode_after_shutdown
        if capture_mode is None:
            return False

        self._pending_capture_mode_after_shutdown = None
        return self._start_engine_for_capture(capture_mode, force_restart=True)

    def _connect_to_engine_if_available(self) -> None:
        if (
            not self._engine_client
            or self._engine_client.is_connected()
            or not self._engine_manager.is_running()
            or not os.path.exists(self._engine_socket_path)
        ):
            return

        self._engine_client.set_auto_reconnect(True)
        self._engine_client.connect()

    def _shutdown_idle_engine(self) -> bool:
        if self._engine_shutdown_pending:
            return True
        if not self._engine_manager.is_running():
            return False

        self._display_capture_sync_pending = False

        if not self._engine_client or not self._engine_client.is_connected():
            self._engine_manager.terminate()
            self._last_engine_exit_status = None
            self._set_window_engine_status(_ENGINE_STATUS_READY)
            self._last_running_entry_key = None
            return True

        self._engine_shutdown_pending = True
        self._idle_engine_exit_expected = True
        self._engine_client.set_auto_reconnect(False)
        if self._engine_client.shutdown(self._on_idle_engine_shutdown_response):
            return True

        self._engine_shutdown_pending = False
        self._idle_engine_exit_expected = False
        self._engine_manager.terminate()
        return True

    def _on_idle_engine_shutdown_response(self, response: dict) -> None:
        self._engine_shutdown_pending = False
        if not response.get("ok"):
            self._idle_engine_exit_expected = False
            error = response.get("error", "Unknown error")
            self._show_error(_("Engine shutdown failed: %(error)s") % {"error": error})

        if self._engine_client:
            self._engine_client.disconnect(auto_reconnect=False)
        engine_exit_code = self._engine_manager.poll()
        if engine_exit_code is not None:
            self._handle_engine_process_exit(engine_exit_code)
        if engine_exit_code is None or engine_exit_code == 0:
            self._last_engine_exit_status = None
            self._set_window_engine_status(_ENGINE_STATUS_READY)
        self._last_running_entry_key = None
        self._start_pending_capture_after_shutdown()

    def _on_status_response(self, response: dict):
        """Handle status response from engine and update tray icon."""
        if not response.get("ok"):
            # Error getting status - show idle
            if self._tray:
                self._tray.set_status(ClipperTray.STATUS_IDLE)
            self._set_window_engine_status(self._idle_window_engine_status())
            return

        # The engine currently returns status fields at the top level. Older
        # callers/tests used a nested "status" object, so accept both shapes.
        status_data = response.get("status")
        if not isinstance(status_data, dict):
            status_data = response

        buffer_active = status_data.get("buffer_active", status_data.get("status") == "active")
        game_hooked = status_data.get("game_hooked", False)
        capture_mode = status_data.get("capture_mode", self._engine_manager.capture_mode)
        running_entry = self._running_whitelist_entry(capture_mode)
        if running_entry is not None:
            self._maybe_learn_game_audio_identity(running_entry)

        if self._display_target_request_pending:
            self._display_target_request_poll_count += 1
            if status_data.get("display_target_cancelled", False):
                self._finish_display_target_request(
                    False, _("Display selection was cancelled.")
                )
            elif status_data.get("display_target_selected", False):
                self._config.load()
                self._finish_display_target_request(
                    True, _("Capture display selected successfully.")
                )
            elif self._display_target_request_poll_count >= 30:
                self._finish_display_target_request(
                    False, _("No display was selected before the picker timed out.")
                )

        if capture_mode == CAPTURE_MODE_DISPLAY:
            self._sync_display_capture_whitelist(buffer_active)

        # Keep tray presentation deliberately binary: the replay buffer is
        # either recording or it is idle. A prestarted game-capture engine is
        # still idle until frames arrive and the replay buffer starts.
        if buffer_active:
            tray_status = ClipperTray.STATUS_RECORDING
        else:
            tray_status = ClipperTray.STATUS_IDLE

        if self._tray:
            self._tray.set_status(tray_status)

        if buffer_active or game_hooked:
            window_status = _ENGINE_STATUS_RECORDING
        else:
            window_status = _ENGINE_STATUS_READY
        self._set_window_engine_status(window_status)

    def _on_display_target_cancelled(self, _event: dict) -> None:
        """Release display-picker UI as soon as the portal reports cancellation."""
        if self._display_target_request_pending:
            self._finish_display_target_request(
                False, _("Display selection was cancelled.")
            )

    def _finish_display_target_request(self, success: bool, message: str) -> None:
        """Finish an immediate display picker request and stop its temporary engine."""
        callback = self._display_target_request_callback
        temporary_engine = self._display_target_request_temporary_engine
        self._display_target_request_pending = False
        self._display_target_request_callback = None
        self._display_target_request_poll_count = 0
        self._display_target_request_temporary_engine = False

        if callback:
            callback(success, message)
        else:
            self._show_toast(message)

        if (
            temporary_engine
            and self._running_whitelist_entry() is None
            and not self._capabilities_probe_requested
        ):
            self._shutdown_idle_engine()

    def _sync_display_capture_whitelist(self, buffer_active: bool) -> None:
        """Start/stop display capture based on the configured whitelist."""
        if (
            self._display_capture_sync_pending
            or not self._engine_client
            or not self._engine_client.is_connected()
        ):
            return

        # Prefer the host monitor's state. Flatpak's process namespace cannot
        # reliably rediscover the host process that caused the engine to start.
        game_running = self._running_whitelist_entry(CAPTURE_MODE_DISPLAY) is not None

        if game_running and not buffer_active:
            self._display_capture_sync_pending = True
            self._engine_client.start_replay_buffer(self._on_display_capture_sync_response)
        elif not game_running and buffer_active:
            self._display_capture_sync_pending = True
            self._engine_client.stop_replay_buffer(self._on_display_capture_sync_response)

    def _on_display_capture_sync_response(self, response: dict) -> None:
        self._display_capture_sync_pending = False
        if response.get("ok"):
            return

        error = response.get("error", "")
        if error in ("already running", "not running"):
            return
        self._show_error(
            _("Display capture sync failed: %(error)s")
            % {"error": error or _("Unknown error")}
        )

    def _set_window_engine_status(self, status: str):
        """Update the main window engine status indicator when the window exists."""
        if status == self._window_engine_status:
            return
        self._window_engine_status = status
        if self.window:
            self.window.set_engine_status(status)

    def _configured_save_hotkey(self) -> str:
        value = self._config.get("save_hotkey", "")
        return value if isinstance(value, str) else ""

    def _configured_whitelist(self) -> list[dict[str, Any]]:
        value = self._config.get("whitelist", [])
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

    # ------------------------------------------------------------------
    # About dialog
    # ------------------------------------------------------------------

    def _show_fruit_drop_game(self) -> None:
        """Present a fresh instance of the hidden fruit-drop game."""
        if self._fruit_drop_window is not None:
            self._fruit_drop_window.destroy()
            self._fruit_drop_window = None

        from suika_game import SuikaGameWindow

        saved_score = self._config.get("fruit_drop_high_score", 0)
        high_score = saved_score if type(saved_score) is int and saved_score >= 0 else 0
        game_window = SuikaGameWindow(
            transient_for=self.window,
            high_score=high_score,
            high_score_changed=self._save_fruit_drop_high_score,
        )
        self._fruit_drop_window = game_window
        game_window.connect("destroy", self._on_fruit_drop_window_destroyed)
        game_window.present()

    def _on_fruit_drop_window_destroyed(self, window) -> None:
        if self._fruit_drop_window is window:
            self._fruit_drop_window = None

    def _save_fruit_drop_high_score(self, score: int) -> None:
        """Persist only genuine new high scores from the easter egg."""
        saved_score = self._config.get("fruit_drop_high_score", 0)
        current_score = saved_score if type(saved_score) is int and saved_score >= 0 else 0
        if score <= current_score:
            return
        try:
            self._config.set("fruit_drop_high_score", score)
        except OSError as error:
            self._log(f"[clipper] WARN: could not save fruit-drop score: {error}")

    def _connect_version_easter_egg(self, about) -> None:
        """Open the game after five rapid taps on libadwaita's version button."""
        version_button = about.get_template_child(Adw.AboutWindow, "version_button")
        if version_button is None:
            return
        click = Gtk.GestureClick()
        click.set_button(1)
        state = {"count": 0, "last_click": 0.0}

        def on_pressed(_gesture, _press_count, _x, _y) -> None:
            now = time.monotonic()
            if now - state["last_click"] > _VERSION_EASTER_EGG_INTERVAL_SECONDS:
                state["count"] = 0
            state["last_click"] = now
            state["count"] += 1
            if state["count"] >= _VERSION_EASTER_EGG_CLICKS:
                state["count"] = 0
                self._show_fruit_drop_game()

        click.connect("pressed", on_pressed)
        version_button.add_controller(click)

    def on_about(self, action, param):
        """Show about dialog"""
        about = Adw.AboutWindow(
            transient_for=self.window,
            application_name="Clipper",
            application_icon=APP_ICON,
            developer_name=_("🦊 Leese"),
            version="1.0.4",
            website="https://github.com/LeeseTheFox/Clipper",
            issue_url="https://github.com/LeeseTheFox/Clipper/issues",
            copyright=_("© 2026 Leese"),
            license_type=Gtk.License.GPL_3_0,
        )
        for row_name in ("website_row", "details_website_row"):
            website_row = about.get_template_child(Adw.AboutWindow, row_name)
            if website_row is not None:
                website_row.set_title(_("GitHub"))
        self._connect_version_easter_egg(about)
        about.present()


def main(argv: list[str] | None = None):
    """Main entry point"""
    normalize_display_auth_env()
    application_argv, start_hidden = _application_argv(argv or sys.argv)
    app = ClipperApplication(
        start_hidden=start_hidden,
        log_buffer=_initial_log_buffer(),
        capabilities_cache=_initial_capabilities_cache(),
    )
    exit_code = app.run(application_argv)
    if app._restart_in_background or app._restart_in_foreground:
        handoff = None
        try:
            if app._capabilities_cache:
                os.environ[_CAPABILITIES_HANDOFF_ENV] = json.dumps(
                    app._capabilities_cache, separators=(",", ":")
                )
            try:
                handoff = create_handoff(app._log_buffer)
                os.environ[_LOG_HANDOFF_FD_ENV] = str(handoff.fileno())
            except OSError as error:
                print(f"[clipper] WARN: could not preserve logs during restart: {error}")
            restart_argv = [sys.executable, os.path.abspath(__file__)]
            if app._restart_in_background:
                restart_argv.append(_BACKGROUND_OPTION)
            os.execv(sys.executable, restart_argv)
        finally:
            os.environ.pop(_CAPABILITIES_HANDOFF_ENV, None)
            os.environ.pop(_LOG_HANDOFF_FD_ENV, None)
            if handoff is not None:
                handoff.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
