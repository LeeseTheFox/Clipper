import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_main_module():
    module_name = "test_main_isolated"
    main_path = Path(__file__).resolve().parents[1] / "ui" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    assert spec is not None
    assert spec.loader is not None

    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "gi",
            "gi.repository",
            "autostart",
            "capture_modes",
            "config",
            "engine_client",
            "engine_manager",
            "game_audio_learning",
            "hotkeys",
            "log_window",
            "logs",
            "main_window",
            "monitor_ipc",
            "monitor_manager",
            "process_watcher",
            "setup_window",
            "sound_feedback",
            "tray",
        )
    }

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace(
        Application=type("Application", (), {"__init__": lambda self, *args, **kwargs: None}),
        AlertDialog=type("AlertDialog", (), {}),
        AboutWindow=type("AboutWindow", (), {}),
    )

    class NotificationStub:
        def __init__(self, title):
            self.title = title
            self.body = None

        @classmethod
        def new(cls, title):
            return cls(title)

        def set_body(self, body):
            self.body = body

    repository.Gio = types.SimpleNamespace(
        ApplicationFlags=types.SimpleNamespace(FLAGS_NONE=0),
        Notification=NotificationStub,
        SimpleAction=type(
            "SimpleAction",
            (),
            {"new": staticmethod(lambda *_args, **_kwargs: None)},
        ),
    )
    repository.GLib = types.SimpleNamespace(idle_add=lambda callback, *args: callback(*args))
    repository.Gtk = types.SimpleNamespace(
        ApplicationInhibitFlags=types.SimpleNamespace(LOGOUT=1),
        License=types.SimpleNamespace(GPL_3_0=0),
    )
    gi.repository = repository

    autostart = types.ModuleType("autostart")
    autostart.AutostartManager = type("AutostartManager", (), {})

    capture_modes = types.ModuleType("capture_modes")
    capture_modes.CAPTURE_MODE_DISPLAY = "display_capture"
    capture_modes.CAPTURE_MODE_GAME = "game_capture"
    capture_modes.DEFAULT_CAPTURE_MODE = "display_capture"
    capture_modes.capture_mode_for_entry = lambda entry: entry.get(
        "capture_mode", "display_capture"
    )

    config = types.ModuleType("config")
    config.ClipperConfig = type("ClipperConfig", (), {})
    config.normalize_clip_sound_volume = lambda value: max(
        0.0, min(2.0, float(value))
    )

    engine_client = types.ModuleType("engine_client")
    engine_client.EngineClient = type("EngineClient", (), {})

    engine_manager = types.ModuleType("engine_manager")
    engine_manager.EngineProcessManager = type("EngineProcessManager", (), {})
    engine_manager.RESTART_EXIT_CODE = 42

    hotkeys = types.ModuleType("hotkeys")
    hotkeys.GlobalHotkeyManager = type("GlobalHotkeyManager", (), {})
    hotkeys.display_hotkey = lambda hotkey: hotkey
    hotkeys.effective_hotkey_label = lambda _preferred, **kwargs: (
        kwargs.get("portal_label") or "Not set"
    )
    hotkeys.shortcut_id_for_hotkey = lambda hotkey: f"binding-{hotkey}"

    log_window = types.ModuleType("log_window")
    log_window.LogWindow = type("LogWindow", (), {})

    logs = types.ModuleType("logs")
    logs.LogBuffer = type("LogBuffer", (), {"__init__": lambda self: None})
    logs.consume_handoff = lambda _file_descriptor: logs.LogBuffer()
    logs.create_handoff = lambda _log_buffer: None

    main_window = types.ModuleType("main_window")
    main_window.MainWindow = type(
        "MainWindow",
        (),
        {
            "ENGINE_STATUS_READY": "ready",
            "ENGINE_STATUS_RECORDING": "recording",
            "ENGINE_STATUS_EXITED": "exited",
            "ENGINE_STATUS_CRASHED": "crashed",
            "ENGINE_STATUS_STOPPED": "ready",
            "ENGINE_STATUS_RUNNING": "ready",
        },
    )

    monitor_ipc = types.ModuleType("monitor_ipc")
    monitor_ipc.MonitorIpcServer = type("MonitorIpcServer", (), {})
    monitor_ipc.default_monitor_socket_path = lambda: Path("/runtime/clipper/monitor.sock")

    monitor_manager = types.ModuleType("monitor_manager")
    monitor_manager.HostMonitorManager = type("HostMonitorManager", (), {})

    process_watcher = types.ModuleType("process_watcher")
    process_watcher.entry_matches_process = lambda *_args, **_kwargs: False
    process_watcher.find_running_obs_studio = lambda *_args, **_kwargs: None
    process_watcher.find_running_whitelist_entry = lambda *_args, **_kwargs: None
    process_watcher.monitor_rule_id = lambda entry, index: (
        f"steam-{entry['appid']}"
        if entry.get("appid")
        else f"path-{index}"
        if entry.get("executable_path")
        else f"rule-{index}"
    )

    setup_window = types.ModuleType("setup_window")
    setup_window.SetupWindow = type("SetupWindow", (), {})

    sound_feedback = types.ModuleType("sound_feedback")
    sound_feedback.ClipSoundPlayer = type("ClipSoundPlayer", (), {})

    tray = types.ModuleType("tray")
    tray.ClipperTray = type(
        "ClipperTray",
        (),
        {
            "STATUS_IDLE": "idle",
            "STATUS_RECORDING": "recording",
        },
    )

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "autostart": autostart,
            "capture_modes": capture_modes,
            "config": config,
            "engine_client": engine_client,
            "engine_manager": engine_manager,
            "hotkeys": hotkeys,
            "log_window": log_window,
            "logs": logs,
            "main_window": main_window,
            "monitor_ipc": monitor_ipc,
            "monitor_manager": monitor_manager,
            "process_watcher": process_watcher,
            "setup_window": setup_window,
            "sound_feedback": sound_feedback,
            "tray": tray,
        }
    )

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


main_module = _load_main_module()
ClipperApplication = main_module.ClipperApplication


def test_session_detection_distinguishes_x11_and_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert main_module._is_x11_session() is True

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert main_module._is_x11_session() is False

    monkeypatch.delenv("XDG_SESSION_TYPE")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":0")
    assert main_module._is_x11_session() is True


def test_version_easter_egg_opens_after_five_quick_presses(monkeypatch):
    class GestureClickStub:
        def __init__(self):
            self.callback = None

        def set_button(self, button):
            assert button == 1

        def connect(self, signal, callback):
            assert signal == "pressed"
            self.callback = callback

    class VersionButtonStub:
        def __init__(self):
            self.controller = None

        def add_controller(self, controller):
            self.controller = controller

    version_button = VersionButtonStub()
    about = types.SimpleNamespace(
        get_template_child=lambda _window_type, name: version_button
        if name == "version_button"
        else None
    )
    app = object.__new__(ClipperApplication)
    openings = []
    app._show_fruit_drop_game = lambda: openings.append(True)
    times = iter((10.0, 10.1, 10.2, 10.3, 10.4))
    monkeypatch.setattr(main_module.Gtk, "GestureClick", GestureClickStub, raising=False)
    monkeypatch.setattr(main_module.time, "monotonic", lambda: next(times))

    app._connect_version_easter_egg(about)
    assert version_button.controller is not None
    for _ in range(5):
        version_button.controller.callback(None, 1, 0, 0)

    assert openings == [True]


def test_fruit_drop_high_score_only_saves_new_records():
    class ScoreConfig:
        def __init__(self):
            self.values = {"fruit_drop_high_score": 12}
            self.saved = []

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value
            self.saved.append((key, value))

    app = object.__new__(ClipperApplication)
    app._config = ScoreConfig()
    app._log = lambda _message: None

    app._save_fruit_drop_high_score(12)
    app._save_fruit_drop_high_score(31)

    assert app._config.saved == [("fruit_drop_high_score", 31)]


def test_fruit_drop_always_starts_with_a_fresh_window(monkeypatch):
    class ExistingWindow:
        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    class NewWindow:
        instance = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.presented = False
            self.destroy_callback = None
            NewWindow.instance = self

        def connect(self, signal, callback):
            assert signal == "destroy"
            self.destroy_callback = callback

        def present(self):
            self.presented = True

    module = types.ModuleType("suika_game")
    module.SuikaGameWindow = NewWindow
    monkeypatch.setitem(sys.modules, "suika_game", module)

    app = object.__new__(ClipperApplication)
    previous_window = ExistingWindow()
    app._fruit_drop_window = previous_window
    app._config = types.SimpleNamespace(get=lambda _key, default: default)
    app.window = None
    app._save_fruit_drop_high_score = lambda _score: None

    app._show_fruit_drop_game()

    assert previous_window.destroyed is True
    assert NewWindow.instance is app._fruit_drop_window
    assert NewWindow.instance is not None
    assert NewWindow.instance.presented is True


def test_presentation_state_records_each_frame_boundary_once(monkeypatch):
    app = make_status_application()
    app._presentation_marks = {"window": 0, "clips": 0}
    app._presentation_action = None
    timestamps = iter((123, 456))
    monkeypatch.setattr(main_module.time, "monotonic_ns", lambda: next(timestamps))

    app._record_presentation_mark("window")
    app._record_presentation_mark("window")
    app._record_presentation_mark("clips")

    assert json.loads(app._presentation_state()) == {
        "pid": main_module.os.getpid(),
        "window": 123,
        "clips": 456,
    }


class ConfigStub:
    def __init__(
        self,
        minimize_to_tray_on_close: bool,
        whitelist: list[dict] | None = None,
        notify_on_clip_saved: bool = True,
        play_sound_on_clip_saved: bool = False,
        clip_sound_volume: float = 1.0,
    ) -> None:
        self._minimize_to_tray_on_close = minimize_to_tray_on_close
        self._whitelist = whitelist or []
        self._notify_on_clip_saved = notify_on_clip_saved
        self._play_sound_on_clip_saved = play_sound_on_clip_saved
        self._clip_sound_volume = clip_sound_volume

    def get(self, key: str, default=None):
        if key == "minimize_to_tray_on_close":
            return self._minimize_to_tray_on_close
        if key == "whitelist":
            return self._whitelist
        if key == "notify_on_clip_saved":
            return self._notify_on_clip_saved
        if key == "play_sound_on_clip_saved":
            return self._play_sound_on_clip_saved
        if key == "clip_sound_volume":
            return self._clip_sound_volume
        if key == "setup_completed":
            return True
        return default


class ResetConfigStub(ConfigStub):
    def __init__(self) -> None:
        super().__init__(False)
        self.factory_reset = False

    def reset_to_factory_settings(self) -> None:
        self.factory_reset = True


class DisplayConfigStub(ConfigStub):
    def __init__(self) -> None:
        super().__init__(False)
        self.cleared_restore_token = False
        self.saved = []
        self.load_calls = 0

    def clear_pipewire_restore_token(self) -> None:
        self.cleared_restore_token = True

    def set(self, key, value) -> None:
        self.saved.append((key, value))

    def load(self) -> None:
        self.load_calls += 1


class AutostartManagerStub:
    def __init__(self) -> None:
        self.requests: list[bool] = []

    def set_enabled(self, enabled: bool, callback) -> None:
        self.requests.append(enabled)
        callback(True, None)


class TrayStub:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class StatusTrayStub:
    def __init__(self) -> None:
        self.status_values: list[str] = []

    def set_status(self, status: str) -> None:
        self.status_values.append(status)


class EngineClientStub:
    def __init__(self) -> None:
        self.disconnected = False
        self.save_calls: list[dict] = []
        self.auto_reconnect_values: list[bool] = []
        self.shutdown_callbacks: list[dict[str, object]] = []
        self.start_replay_callbacks: list[object] = []
        self.stop_replay_callbacks: list[object] = []

    def disconnect(self, auto_reconnect: bool = True) -> None:
        self.disconnected = True

    def is_connected(self) -> bool:
        return not self.disconnected

    def connect(self) -> None:
        self.disconnected = False

    def set_auto_reconnect(self, value: bool) -> None:
        self.auto_reconnect_values.append(value)

    def shutdown(self, callback, restart: bool = False) -> bool:
        self.shutdown_callbacks.append({"callback": callback, "restart": restart})
        return True

    def get_status(self, callback) -> bool:
        callback({"ok": True, "buffer_active": False, "capture_mode": "display_capture"})
        return True

    def save_replay_buffer(self, callback, game_name=None) -> bool:
        self.save_calls.append({"callback": callback, "game_name": game_name})
        return True

    def start_replay_buffer(self, callback) -> bool:
        self.start_replay_callbacks.append(callback)
        return True

    def stop_replay_buffer(self, callback) -> bool:
        self.stop_replay_callbacks.append(callback)
        return True


class EngineManagerStub:
    capture_mode = "game_capture"

    def __init__(self, *, exit_code=None, running: bool = False) -> None:
        self._exit_code = exit_code
        self._running = running
        self.terminated = False
        self.start_calls: list[str] = []

    def poll(self):
        exit_code = self._exit_code
        self._exit_code = None
        if exit_code is not None:
            self._running = False
        return exit_code

    def is_running(self) -> bool:
        return self._running

    def start(self, capture_mode: str) -> bool:
        self.start_calls.append(capture_mode)
        self.capture_mode = capture_mode
        self._running = True
        return True

    def terminate(self) -> None:
        self.terminated = True
        self._running = False


class MonitorManagerStub:
    def __init__(
        self,
        *,
        available: bool = True,
        start_ok: bool = True,
    ) -> None:
        self._available = available
        self._start_ok = start_ok
        self.started = False
        self.stopped = False

    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        self.started = True
        return self._start_ok

    def stop(self) -> None:
        self.stopped = True

    def is_running(self) -> bool:
        return self.started and not self.stopped


class WindowStub:
    def __init__(self) -> None:
        self.visible_values: list[bool] = []
        self.cleanup_calls = 0

    def set_visible(self, value: bool) -> None:
        self.visible_values.append(value)

    def cleanup(self) -> None:
        self.cleanup_calls += 1


def make_application(
    *,
    minimize_to_tray_on_close: bool,
    tray_available: bool,
    engine_running: bool = False,
) -> ClipperApplication:
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(minimize_to_tray_on_close)
    app._tray = TrayStub(tray_available)
    app._engine_manager = EngineManagerStub(running=engine_running)
    app._restart_in_background = False
    app.window = WindowStub()
    app._quit_calls = 0
    app.quit = lambda: setattr(app, "_quit_calls", app._quit_calls + 1)
    app._refresh_calls = 0
    app._refresh_tray_menu = lambda: setattr(app, "_refresh_calls", app._refresh_calls + 1)
    return app


def make_status_application(
    *, engine_manager: EngineManagerStub | None = None
) -> ClipperApplication:
    app = object.__new__(ClipperApplication)
    app._start_hidden = False
    app._background_hold = False
    app.window = None
    app._config = ConfigStub(minimize_to_tray_on_close=False)
    app._tray = None
    app._engine_client = None
    app._engine_socket_path = "/missing/clipper-engine.sock"
    app._engine_manager = engine_manager or EngineManagerStub()
    app._window_engine_status = "ready"
    app._display_capture_sync_pending = False
    app._engine_shutdown_pending = False
    app._idle_engine_exit_expected = False
    app._last_engine_exit_status = None
    app._capabilities_probe_requested = False
    app._capabilities_probe_count = 0
    app._capabilities_cache = {}
    app._pending_capture_mode_after_shutdown = None
    app._pending_hotkey_change = None
    app._last_running_entry_key = None
    app._monitor_active_rules = {}
    app._obs_studio_running = False
    app._obs_conflict_dialog_shown = False
    app._obs_conflict_notification_sent = False
    app._startup_diagnostics_logged = False
    app.setup_window = None
    app._display_target_request_pending = False
    app._display_target_request_callback = None
    app._display_target_request_poll_count = 0
    app._display_target_request_temporary_engine = False
    app._monitor_manager = MonitorManagerStub()
    app._monitor_manager.started = True
    app._monitor_ipc = None
    app._monitor_socket_path = Path("/runtime/clipper/monitor.sock")
    app._show_error = lambda message: None
    return app


def test_close_request_restarts_idle_window_in_background():
    app = make_application(minimize_to_tray_on_close=True, tray_available=True)

    handled = app._on_window_close_request(None)

    assert handled is True
    assert app.window.visible_values == []
    assert app._restart_in_background is True
    assert app._quit_calls == 1
    assert app._refresh_calls == 0


def test_close_request_only_hides_window_while_engine_is_running():
    app = make_application(
        minimize_to_tray_on_close=True,
        tray_available=True,
        engine_running=True,
    )

    handled = app._on_window_close_request(None)

    assert handled is True
    assert app.window.visible_values == [False]
    assert app._restart_in_background is False
    assert app._quit_calls == 0
    assert app._refresh_calls == 1


def test_close_request_allows_normal_close_when_tray_hide_is_unavailable():
    app = make_application(minimize_to_tray_on_close=True, tray_available=False)
    window = app.window

    handled = app._on_window_close_request(None)

    assert handled is False
    assert window.visible_values == []
    assert window.cleanup_calls == 1
    assert app.window is None
    assert app._refresh_calls == 0


def test_close_request_retires_window_when_minimize_to_tray_is_disabled():
    app = make_application(minimize_to_tray_on_close=False, tray_available=True)
    window = app.window

    handled = app._on_window_close_request(None)

    assert handled is False
    assert window.cleanup_calls == 1
    assert app.window is None
    assert app._restart_in_background is False
    assert app._quit_calls == 0


def test_tray_quit_cancels_pending_background_restart():
    app = make_application(minimize_to_tray_on_close=True, tray_available=True)
    app._restart_in_background = True

    app._on_tray_quit()

    assert app._restart_in_background is False
    assert app._quit_calls == 1


def test_background_update_check_does_not_prevent_quitting():
    app = make_application(minimize_to_tray_on_close=True, tray_available=True)
    shown = []
    app._updater = types.SimpleNamespace(busy=True, _checking=True, show=lambda: shown.append(True))
    app.request_quit()
    assert app._quit_calls == 1
    assert not shown


def test_update_installation_prevents_quitting():
    app = make_application(minimize_to_tray_on_close=True, tray_available=True)
    shown = []
    app._updater = types.SimpleNamespace(
        busy=True, _checking=False, show=lambda: shown.append(True)
    )
    app.request_quit()
    assert app._quit_calls == 0
    assert shown == [True]


def test_session_end_routes_through_editor_close_prompt():
    app = make_application(minimize_to_tray_on_close=False, tray_available=True)

    class EditorStub:
        def __init__(self):
            self.present_calls = 0
            self.cancelled_callback = None

        def present(self):
            self.present_calls += 1

        def request_close(self, *, cancelled_callback=None):
            self.cancelled_callback = cancelled_callback

    editor = EditorStub()
    app.editor_window = editor
    app._session_end_prompt_pending = False
    app._quit_after_editor = False

    app._on_session_query_end()

    assert editor.present_calls == 1
    assert editor.cancelled_callback is not None
    assert app._session_end_prompt_pending is True
    assert app._quit_after_editor is True
    assert app._quit_calls == 0

    editor.cancelled_callback()

    assert app._session_end_prompt_pending is False
    assert app._quit_after_editor is False


def test_session_end_without_editor_quits_immediately():
    app = make_application(minimize_to_tray_on_close=False, tray_available=True)
    app.editor_window = None

    app._on_session_query_end()

    assert app._quit_calls == 1


def test_editor_logout_inhibitor_is_released_once():
    app = make_application(minimize_to_tray_on_close=False, tray_available=True)
    app._editor_logout_inhibit_cookie = 0
    inhibit_calls = []
    uninhibit_calls = []
    app.inhibit = lambda window, flags, reason: inhibit_calls.append(
        (window, flags, reason)
    ) or 37
    app.uninhibit = uninhibit_calls.append
    editor = object()

    app._inhibit_logout_for_editor(editor)
    app._inhibit_logout_for_editor(editor)
    app._release_editor_logout_inhibitor()
    app._release_editor_logout_inhibitor()

    assert inhibit_calls == [
        (editor, 1, "Save or discard the video edit before logging out")
    ]
    assert uninhibit_calls == [37]
    assert app._editor_logout_inhibit_cookie == 0


def test_log_window_is_weakly_owned_and_replaced_after_close_starts():
    original_log_window = main_module.LogWindow
    created = []

    class LogWindowStub:
        def __init__(self, log_buffer, transient_for=None):
            self.log_buffer = log_buffer
            self.transient_for = transient_for
            self._destroyed = False
            self.present_calls = 0
            created.append(self)

        def present(self):
            self.present_calls += 1

    main_module.LogWindow = LogWindowStub
    try:
        app = object.__new__(ClipperApplication)
        app._log_buffer = object()
        app.window = object()

        app.on_show_logs(None, None)
        app.on_show_logs(None, None)

        assert len(created) == 1
        assert created[0].present_calls == 2
        assert app._log_window_ref() is created[0]
        assert not hasattr(app, "_log_window")

        created[0]._destroyed = True
        app.on_show_logs(None, None)

        assert len(created) == 2
        assert app._log_window_ref() is created[1]
        assert created[1].present_calls == 1
    finally:
        main_module.LogWindow = original_log_window


def test_factory_reset_refreshes_window_and_reopens_setup():
    app = object.__new__(ClipperApplication)
    app._config = ResetConfigStub()
    app._autostart_manager = AutostartManagerStub()
    app._register_save_hotkey = lambda _hotkey: None
    app._configured_save_hotkey = lambda: ""
    app._restart_engine = lambda _reason: None
    app._engine_client = None
    app._engine_manager = EngineManagerStub()
    setup_calls = []
    app._present_setup_window = lambda: setup_calls.append(True)

    class Window:
        def __init__(self) -> None:
            self.reload_calls = 0
            self.visible_values = []

        def reload_from_config(self) -> None:
            self.reload_calls += 1

        def set_visible(self, value: bool) -> None:
            self.visible_values.append(value)

    app.window = Window()

    app.on_reset_factory_settings(None, None)

    assert app._config.factory_reset is True
    assert app._autostart_manager.requests == [False]
    assert app.window.reload_calls == 1
    assert app.window.visible_values == [False]
    assert setup_calls == [True]


def test_window_has_no_host_monitor_controls(monkeypatch):
    app = make_status_application()
    app._monitor_manager = MonitorManagerStub(available=False)
    app._refresh_tray_menu = lambda: None
    created = {}

    class MainWindowStub:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def set_engine_status(self, _status):
            pass

        def connect(self, *_args):
            pass

        def present(self):
            pass

    monkeypatch.setattr(main_module, "MainWindow", MainWindowStub)

    app.do_activate()

    assert not any(key.startswith("monitor_") for key in created)
    assert "hotkey_configure_callback" not in created


def test_background_activation_starts_services_without_creating_window(monkeypatch):
    app = make_status_application()
    app._start_hidden = True
    app._refresh_tray_menu = lambda: None
    held = []
    app.hold = lambda: held.append(True)
    released = []
    app.release = lambda: released.append(True)
    presented = []

    class MainWindowStub:
        def __init__(self, **_kwargs):
            pass

        def set_engine_status(self, _status):
            pass

        def connect(self, *_args):
            pass

        def present(self):
            presented.append(True)

    monkeypatch.setattr(main_module, "MainWindow", MainWindowStub)

    app.do_activate()

    assert app.window is None
    assert held == [True]
    assert presented == []
    assert app._start_hidden is False
    assert app._background_hold is True
    assert released == []

    app.do_activate()
    assert app.window is not None
    assert presented == [True]
    assert released == [True]
    assert app._background_hold is False


def test_first_activation_presents_multistep_setup_instead_of_main_window(monkeypatch):
    app = make_status_application()
    app._config = ConfigStub(False)
    app._config.get = lambda key, default=None: (
        False if key == "setup_completed" else ConfigStub.get(app._config, key, default)
    )
    created = []

    class SetupWindowStub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.presented = False
            created.append(self)

        def connect(self, *_args):
            pass

        def present(self):
            self.presented = True

    monkeypatch.setattr(main_module, "SetupWindow", SetupWindowStub)

    app.do_activate()

    assert app.window is None
    assert len(created) == 1
    assert created[0].presented is True
    assert created[0].kwargs["display_target_callback"] == app.change_capture_target_display
    assert created[0].kwargs["hotkey_changed_callback"] == app._on_save_hotkey_changed
    assert created[0].kwargs["hotkey_capture_state_callback"] == app._set_hotkey_capture_active


def test_x11_activation_hides_display_selection_controls(monkeypatch):
    app = make_status_application()
    app._config = ConfigStub(False)
    app._config.get = lambda key, default=None: (
        False if key == "setup_completed" else ConfigStub.get(app._config, key, default)
    )
    app._is_x11 = True
    created = []

    class SetupWindowStub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def connect(self, *_args):
            pass

        def present(self):
            pass

    monkeypatch.setattr(main_module, "SetupWindow", SetupWindowStub)

    app.do_activate()

    assert created[0].kwargs["show_display_target_controls"] is False


def test_global_hotkey_is_ignored_while_shortcut_capture_is_active():
    app = make_status_application()
    saved = []
    accepted = []
    app._on_tray_save_clip = lambda: saved.append(True)

    app._hotkey_capture_active = True
    app._accept_current_hotkey_capture = lambda: accepted.append(True)
    app._on_global_save_hotkey()

    assert saved == []
    assert accepted == [True]

    app._hotkey_capture_active = False
    app._accept_current_hotkey_capture = None
    app._on_global_save_hotkey()

    assert saved == [True]


def test_hotkey_capture_suppression_has_a_portal_delivery_grace_period(monkeypatch):
    app = make_status_application()
    app._hotkey_capture_active = False
    app._hotkey_capture_release_id = None
    scheduled = []

    monkeypatch.setattr(
        main_module.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 42,
        raising=False,
    )

    app._set_hotkey_capture_active(True)
    app._set_hotkey_capture_active(False)

    assert app._hotkey_capture_active is True
    assert app._hotkey_capture_release_id == 42
    assert scheduled[0][0] == main_module._HOTKEY_CAPTURE_RELEASE_DELAY_MS

    assert scheduled[0][1]() is False
    assert app._hotkey_capture_active is False
    assert app._hotkey_capture_release_id is None


def test_locally_changed_hotkey_requests_its_portal_binding_immediately():
    app = make_status_application()

    class HotkeyConfigStub(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {"setup_completed": True}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    app._config = HotkeyConfigStub()

    class HotkeyManagerStub:
        def __init__(self):
            self.set_values = []

        @property
        def registration_pending(self):
            return False

        @property
        def backend_name(self):
            return "XDG GlobalShortcuts portal"

        def set_hotkey(self, value, shortcut_id):
            self.set_values.append((value, shortcut_id))
            return True

    manager = HotkeyManagerStub()
    app._hotkey_manager = manager
    app._log = lambda _message: None

    app._on_save_hotkey_changed("F15")

    binding_id = main_module.shortcut_id_for_hotkey("F15")
    assert manager.set_values == [("F15", binding_id)]
    assert ("hotkey_binding_id", binding_id) in app._config.saved


def test_selecting_registered_hotkey_is_accepted_without_registration():
    app = make_status_application()

    class HotkeyConfigStub(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {"save_hotkey": "ctrl+alt+s"}

        def get(self, key, default=None):
            return self.values.get(key, default)

    class HotkeyManagerStub:
        backend_name = "XDG GlobalShortcuts portal"
        registration_pending = False

        def __init__(self):
            self.set_values = []

        def set_hotkey(self, value, shortcut_id):
            self.set_values.append((value, shortcut_id))
            return True

    app._config = HotkeyConfigStub()
    manager = HotkeyManagerStub()
    app._hotkey_manager = manager

    assert app._on_save_hotkey_changed("ctrl+alt+s") is True
    assert manager.set_values == []
    assert app._pending_hotkey_change is None


def test_setup_requests_portal_registration_as_soon_as_hotkey_is_selected():
    app = make_status_application()

    class SetupConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {"setup_completed": False}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    class HotkeyManagerStub:
        def __init__(self):
            self.set_values = []

        @property
        def registration_pending(self):
            return True

        @property
        def backend_name(self):
            return "XDG GlobalShortcuts portal"

        def set_hotkey(self, value, shortcut_id):
            self.set_values.append((value, shortcut_id))
            return True

    app._config = SetupConfig()
    manager = HotkeyManagerStub()
    app._hotkey_manager = manager
    app._log = lambda _message: None

    app._on_save_hotkey_changed("ctrl+bracketleft")

    binding_id = main_module.shortcut_id_for_hotkey("ctrl+bracketleft")
    assert manager.set_values == [("ctrl+bracketleft", binding_id)]


def test_portal_shortcut_change_preserves_human_readable_layout_label():
    app = make_status_application()

    class PortalConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {"save_hotkey": "ctrl+alt+s"}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    class SettingsViewStub:
        def __init__(self):
            self.values = []

        def set_save_hotkey_from_portal(self, value):
            self.values.append(value)

    app._config = PortalConfig()
    settings = SettingsViewStub()
    app.window = types.SimpleNamespace(settings_view=settings)
    app._log = lambda _message: None

    app._on_global_hotkey_status("changed", "Strg+Ö")

    assert app._config.values["save_hotkey"] == "ctrl+alt+s"
    assert app._config.values["save_hotkey_portal_managed"] is True
    assert app._config.values["save_hotkey_portal_label"] == "Strg+Ö"
    assert settings.values == ["Strg+Ö"]


def test_portal_can_remove_effective_shortcut_without_losing_preference():
    app = make_status_application()

    class PortalConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {
                "save_hotkey": "F16",
                "save_hotkey_portal_managed": True,
                "save_hotkey_portal_label": "F16",
            }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    app._config = PortalConfig()
    app._log = lambda _message: None

    app._on_global_hotkey_status("changed", None)

    assert app._config.values["save_hotkey"] == "F16"
    assert app._config.values["save_hotkey_portal_managed"] is True
    assert app._config.values["save_hotkey_portal_label"] == ""


def test_portal_rejection_reports_old_binding_as_inactive_without_second_request():
    app = make_status_application()

    class PortalConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {
                "save_hotkey": "ctrl+alt+s",
                "hotkey_binding_id": "binding-old",
                "save_hotkey_portal_managed": True,
                "save_hotkey_portal_label": "Ctrl+Alt+S",
            }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    class HotkeyManagerStub:
        backend_name = "XDG GlobalShortcuts portal"
        registration_pending = True

        def __init__(self):
            self.set_values = []

        def set_hotkey(self, value, shortcut_id):
            self.set_values.append((value, shortcut_id))
            return True

    class SettingsViewStub:
        def __init__(self):
            self.refreshes = 0

        def refresh_save_hotkey_from_config(self):
            self.refreshes += 1

    app._config = PortalConfig()
    manager = HotkeyManagerStub()
    app._hotkey_manager = manager
    settings = SettingsViewStub()
    app.window = types.SimpleNamespace(settings_view=settings)
    app._log = lambda _message: None

    assert app._on_save_hotkey_changed("ctrl+bracketleft") is True
    assert app._config.values["save_hotkey"] == "ctrl+alt+s"
    assert app._config.values["hotkey_binding_id"] == "binding-old"
    app._on_global_hotkey_error("Portal request was denied or cancelled (1)")

    assert app._config.values["save_hotkey"] == "ctrl+alt+s"
    assert app._config.values["hotkey_binding_id"] == "binding-old"
    assert app._config.values["save_hotkey_portal_managed"] is True
    assert app._config.values["save_hotkey_portal_label"] == ""
    assert settings.refreshes == 1
    assert manager.set_values == [
        (
            "ctrl+bracketleft",
            main_module.shortcut_id_for_hotkey("ctrl+bracketleft"),
        )
    ]


def test_portal_approval_commits_requested_binding_and_effective_label():
    app = make_status_application()

    class PortalConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {
                "save_hotkey": "ctrl+alt+s",
                "hotkey_binding_id": "binding-old",
                "save_hotkey_portal_managed": True,
                "save_hotkey_portal_label": "Ctrl+Alt+S",
            }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    class HotkeyManagerStub:
        backend_name = "XDG GlobalShortcuts portal"
        registration_pending = True

        def set_hotkey(self, _value, _shortcut_id):
            return True

    app._config = PortalConfig()
    app._hotkey_manager = HotkeyManagerStub()
    app._log = lambda _message: None

    assert app._on_save_hotkey_changed("ctrl+bracketleft") is True
    app._on_global_hotkey_status("registered", "Ctrl+[")

    assert app._config.values["save_hotkey"] == "ctrl+bracketleft"
    assert app._config.values["hotkey_binding_id"] == (
        main_module.shortcut_id_for_hotkey("ctrl+bracketleft")
    )
    assert app._config.values["save_hotkey_portal_managed"] is True
    assert app._config.values["save_hotkey_portal_label"] == "Ctrl+["


def test_first_portal_rejection_leaves_shortcut_not_set():
    app = make_status_application()

    class PortalConfig(DisplayConfigStub):
        def __init__(self):
            super().__init__()
            self.values = {
                "save_hotkey": "ctrl+alt+s",
                "hotkey_binding_id": "save-replay-buffer",
                "save_hotkey_portal_managed": False,
                "save_hotkey_portal_label": "",
            }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            super().set(key, value)
            self.values[key] = value

    class HotkeyManagerStub:
        def __init__(self):
            self.backend_name = None
            self.registration_pending = False
            self.set_values = []

        def set_hotkey(self, value, shortcut_id):
            self.set_values.append((value, shortcut_id))
            self.backend_name = "XDG GlobalShortcuts portal"
            self.registration_pending = True
            return True

    app._config = PortalConfig()
    manager = HotkeyManagerStub()
    app._hotkey_manager = manager
    app._log = lambda _message: None

    assert app._on_save_hotkey_changed("F15") is True
    app._on_global_hotkey_error("Portal request was denied or cancelled (1)")

    assert app._config.values["save_hotkey"] == ""
    assert app._config.values["hotkey_binding_id"] == "save-replay-buffer"
    assert app._config.values["save_hotkey_portal_managed"] is False
    assert manager.set_values == [("F15", main_module.shortcut_id_for_hotkey("F15"))]


def test_closing_incomplete_setup_quits_instead_of_leaving_tray_process():
    app = make_status_application()
    app._config = ConfigStub(False)
    app._config.get = lambda key, default=None: (
        False if key == "setup_completed" else ConfigStub.get(app._config, key, default)
    )
    setup_window = object()
    app.setup_window = setup_window
    quit_calls = []
    app.quit = lambda: quit_calls.append(True)

    handled = app._on_setup_window_closed(setup_window)

    assert handled is False
    assert app.setup_window is None
    assert quit_calls == [True]


def test_programmatic_setup_close_after_completion_does_not_quit():
    app = make_status_application()
    closing_window = object()
    app.setup_window = None
    quit_calls = []
    app.quit = lambda: quit_calls.append(True)

    handled = app._on_setup_window_closed(closing_window)

    assert handled is False
    assert quit_calls == []


def test_display_target_button_opens_wayland_picker_engine_immediately(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    engine_manager = EngineManagerStub()
    app = make_status_application(engine_manager=engine_manager)
    app._config = DisplayConfigStub()
    results = []

    started = app.change_capture_target_display(
        lambda success, message: results.append((success, message))
    )

    assert started is True
    assert app._config.cleared_restore_token is True
    assert ("display_capture_guidance_seen", True) in app._config.saved
    assert engine_manager.start_calls == ["display_capture"]
    assert app._display_target_request_pending is True

    app._on_status_response(
        {
            "ok": True,
            "capture_mode": "display_capture",
            "buffer_active": False,
            "display_target_selected": True,
        }
    )

    assert results == [(True, "Capture display selected successfully.")]
    assert app._config.load_calls == 1
    assert app._display_target_request_pending is False
    assert engine_manager.terminated is True


def test_display_picker_cancellation_releases_pending_request_immediately(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    engine_manager = EngineManagerStub()
    app = make_status_application(engine_manager=engine_manager)
    app._config = DisplayConfigStub()
    results = []

    app.change_capture_target_display(lambda success, message: results.append((success, message)))
    app._on_display_target_cancelled({"event": "display_target_cancelled"})

    assert results == [(False, "Display selection was cancelled.")]
    assert app._display_target_request_pending is False
    assert engine_manager.terminated is True


def test_display_target_button_explains_x11_has_no_system_picker(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    engine_manager = EngineManagerStub()
    app = make_status_application(engine_manager=engine_manager)
    app._config = DisplayConfigStub()
    results = []

    started = app.change_capture_target_display(
        lambda success, message: results.append((success, message))
    )

    assert started is False
    assert results and results[0][0] is False
    assert "X11" in results[0][1]
    assert engine_manager.start_calls == []


def test_background_argument_is_removed_before_gapplication_parsing():
    argv, start_hidden = main_module._application_argv(
        ["main.py", "--background", "--gapplication-service"]
    )

    assert argv == ["main.py", "--gapplication-service"]
    assert start_hidden is True


def test_capabilities_cache_is_restored_only_from_background_handoff(monkeypatch):
    cached = {
        "ok": True,
        "formats": ["mkv"],
        "video_encoders": [{"id": "obs_x264"}],
        "audio_encoders": [{"id": "ffmpeg_aac"}],
    }
    monkeypatch.setenv(
        main_module._CAPABILITIES_HANDOFF_ENV,
        main_module.json.dumps(cached),
    )

    assert main_module._initial_capabilities_cache() == cached
    assert main_module._CAPABILITIES_HANDOFF_ENV not in main_module.os.environ
    assert main_module._initial_capabilities_cache() == {}


def test_existing_autostart_registration_is_migrated_to_background_mode():
    app = object.__new__(ClipperApplication)

    class Config:
        def __init__(self):
            self.data = {
                "start_on_boot": True,
                "autostart_background_mode_configured": False,
            }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def set(self, key, value):
            self.data[key] = value

    app._config = Config()
    app._autostart_manager = AutostartManagerStub()
    app._start_hidden = False

    app._migrate_background_autostart_command()

    assert app._autostart_manager.requests == [True]
    assert app._config.data["autostart_background_mode_configured"] is True
    assert app._start_hidden is True


def test_current_or_disabled_autostart_registration_needs_no_migration():
    app = object.__new__(ClipperApplication)

    class Config:
        def __init__(self, start_on_boot, configured):
            self.data = {
                "start_on_boot": start_on_boot,
                "autostart_background_mode_configured": configured,
            }

        def get(self, key, default=None):
            return self.data.get(key, default)

    app._autostart_manager = AutostartManagerStub()
    app._start_hidden = False
    for start_on_boot, configured in ((False, False), (True, True)):
        app._config = Config(start_on_boot, configured)
        app._migrate_background_autostart_command()

    assert app._autostart_manager.requests == []
    assert app._start_hidden is False


def test_tray_save_clip_uses_displayed_whitelist_game_name():
    app = make_status_application()
    client = EngineClientStub()
    app._engine_client = client
    app._running_whitelist_entry = lambda capture_mode=None: {
        "name": "Counter-Strike 2",
        "appid": "730",
    }
    app._on_clip_saved = lambda response: None

    app._on_tray_save_clip()

    assert client.save_calls == [{"callback": app._on_clip_saved, "game_name": "Counter-Strike 2"}]


def test_clip_saved_shows_notification_when_enabled():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(False, notify_on_clip_saved=True)
    notifications = []
    app.send_notification = lambda notification_id, notification: notifications.append(
        (notification_id, notification.title, notification.body)
    )

    app._on_clip_saved({"ok": True})

    assert notifications == [("clip-captured", "Clip captured", "Your clip has been saved.")]


def test_clip_saved_refreshes_open_clips_view():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(False, notify_on_clip_saved=False)
    refreshed = []
    app.window = types.SimpleNamespace(
        clips_view=types.SimpleNamespace(refresh=lambda: refreshed.append(True))
    )

    app._on_clip_saved({"ok": True})

    assert refreshed == [True]


def test_clip_saved_without_window_does_not_require_refresh():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(False, notify_on_clip_saved=False)

    app._on_clip_saved({"ok": True})


def test_failed_clip_does_not_refresh_clips_view():
    app = make_status_application()
    refreshed = []
    app.window = types.SimpleNamespace(
        clips_view=types.SimpleNamespace(refresh=lambda: refreshed.append(True))
    )

    app._on_clip_saved({"ok": False, "error": "Save failed"})

    assert refreshed == []


def test_update_restart_blocks_new_saves_and_editor_loading():
    app = object.__new__(ClipperApplication)
    app._update_restart_pending = True
    finished = []
    # Neither operation should need an engine, editor, or worker after reservation.
    app._on_tray_save_clip()
    app.open_editor("unused.mkv", lambda: finished.append(True))
    assert finished == [True]


def test_clip_saved_does_not_show_notification_when_disabled():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(False, notify_on_clip_saved=False)
    app.send_notification = lambda *_args: (_ for _ in ()).throw(
        AssertionError("notification should not be sent")
    )

    app._on_clip_saved({"ok": True})


def test_clip_saved_plays_sound_when_enabled():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(
        False, play_sound_on_clip_saved=True, clip_sound_volume=1.6
    )
    app._clip_sound_player = types.SimpleNamespace(play=lambda _volume: True)
    played = []
    app._clip_sound_player.play = lambda volume: played.append(volume) or True
    app.send_notification = lambda *_args: None

    app._on_clip_saved({"ok": True})

    assert played == [1.6]


def test_clip_saved_does_not_play_sound_when_disabled():
    app = object.__new__(ClipperApplication)
    app._config = ConfigStub(False, play_sound_on_clip_saved=False)
    app._clip_sound_player = types.SimpleNamespace(
        play=lambda: (_ for _ in ()).throw(AssertionError("sound should not play"))
    )
    app.send_notification = lambda *_args: None

    app._on_clip_saved({"ok": True})


def test_failed_clip_does_not_play_success_sound():
    app = make_status_application()
    app._config = ConfigStub(False, play_sound_on_clip_saved=True)
    app._clip_sound_player = types.SimpleNamespace(
        play=lambda: (_ for _ in ()).throw(AssertionError("success sound should not play"))
    )

    app._on_clip_saved({"ok": False, "error": "Save failed"})


def test_idle_engine_status_defaults_to_ready():
    app = make_status_application()

    assert app._poll_engine_status() is True

    assert app._window_engine_status == "ready"


def test_game_capture_rule_does_not_start_engine_before_game_starts():
    entry = {
        "name": "Bloons TD 6",
        "appid": "960090",
        "capture_mode": "game_capture",
    }
    engine_manager = EngineManagerStub(running=False)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])

    assert app._poll_engine_status() is True

    assert engine_manager.start_calls == []
    assert engine_manager.is_running() is False


def test_inactive_game_capture_rule_does_not_keep_engine_running():
    entry = {
        "name": "Bloons TD 6",
        "appid": "960090",
        "capture_mode": "game_capture",
    }
    engine_manager = EngineManagerStub(running=True)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])

    assert app._poll_engine_status() is True

    assert engine_manager.terminated is True
    assert engine_manager.is_running() is False


def test_display_only_rules_do_not_prearm_game_capture_engine():
    entry = {
        "name": "Feishin",
        "path": "/app/main/feishin",
        "capture_mode": "display_capture",
    }
    engine_manager = EngineManagerStub(running=False)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])

    assert app._poll_engine_status() is True

    assert engine_manager.start_calls == []


def test_fresh_inactive_game_capture_engine_is_idle_and_ready():
    app = make_status_application()
    app._tray = StatusTrayStub()

    app._on_status_response(
        {
            "ok": True,
            "buffer_active": False,
            "game_hooked": False,
            "capture_mode": "game_capture",
        }
    )

    assert app._window_engine_status == "ready"
    assert app._tray.status_values == ["idle"]


def test_game_capture_returns_to_same_idle_tray_state_after_recording():
    app = make_status_application()
    app._tray = StatusTrayStub()

    app._on_status_response(
        {
            "ok": True,
            "buffer_active": True,
            "game_hooked": True,
            "capture_mode": "game_capture",
        }
    )
    app._on_status_response(
        {
            "ok": True,
            "buffer_active": False,
            "game_hooked": False,
            "capture_mode": "game_capture",
        }
    )

    assert app._window_engine_status == "ready"
    assert app._tray.status_values == ["recording", "idle"]


def test_active_display_capture_is_recording_in_tray():
    app = make_status_application()
    app._tray = StatusTrayStub()

    app._on_status_response(
        {
            "ok": True,
            "buffer_active": True,
            "game_hooked": False,
            "capture_mode": "display_capture",
        }
    )

    assert app._window_engine_status == "recording"
    assert app._tray.status_values == ["recording"]


def test_active_game_capture_is_recording_in_tray():
    app = make_status_application()
    app._tray = StatusTrayStub()

    app._on_status_response(
        {
            "ok": True,
            "buffer_active": True,
            "game_hooked": True,
            "capture_mode": "game_capture",
        }
    )

    assert app._window_engine_status == "recording"
    assert app._tray.status_values == ["recording"]


def test_unexpected_engine_exit_marks_window_crashed():
    app = make_status_application(engine_manager=EngineManagerStub(exit_code=1))

    assert app._poll_engine_status() is True

    assert app._window_engine_status == "crashed"


def test_disconnect_error_marks_unexpected_engine_exit_crashed():
    app = make_status_application(engine_manager=EngineManagerStub(exit_code=1))

    app._on_engine_error("Engine disconnected")

    assert app._window_engine_status == "crashed"


def test_delayed_idle_shutdown_exit_keeps_window_ready():
    engine_manager = EngineManagerStub(running=True)
    app = make_status_application(engine_manager=engine_manager)
    app._engine_client = EngineClientStub()
    app._engine_shutdown_pending = True
    app._idle_engine_exit_expected = True

    app._on_idle_engine_shutdown_response({"ok": True})
    engine_manager._exit_code = 0
    app._poll_engine_status()

    assert app._window_engine_status == "ready"
    assert app._last_engine_exit_status is None


def test_restart_exit_code_does_not_mark_window_crashed():
    app = make_status_application(engine_manager=EngineManagerStub(exit_code=42))

    assert app._poll_engine_status() is True

    assert app._window_engine_status == "ready"


def test_concurrent_capability_probes_keep_temporary_engine_until_all_finish():
    engine_manager = EngineManagerStub(running=True)
    app = make_status_application(engine_manager=engine_manager)

    assert app.ensure_engine_for_capabilities() is True
    assert app.ensure_engine_for_capabilities() is True

    app.on_capabilities_finished()

    assert app._capabilities_probe_requested is True
    assert engine_manager.terminated is False

    app.on_capabilities_finished()

    assert app._capabilities_probe_requested is False
    assert engine_manager.terminated is True


def test_capability_probe_stops_with_inactive_game_capture_rule():
    entry = {
        "name": "Bloons TD 6",
        "appid": "960090",
        "capture_mode": "game_capture",
    }
    engine_manager = EngineManagerStub(running=True)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])
    app._capabilities_probe_requested = True
    app._capabilities_probe_count = 1

    app.on_capabilities_finished()

    assert app._capabilities_probe_requested is False
    assert engine_manager.terminated is True


def test_running_whitelist_entry_uses_monitor_event_for_display_capture():
    entry = {
        "name": "Counter-Strike 2",
        "appid": "730",
        "capture_mode": "display_capture",
    }
    app = make_status_application()
    app._config = ConfigStub(False, whitelist=[entry])
    app._monitor_active_rules = {"steam-730": {"event": "process_started"}}

    assert app._running_whitelist_entry("display_capture") == entry


def test_running_whitelist_entry_filters_host_events_by_capture_mode():
    game_entry = {
        "name": "Crab Game",
        "appid": "1782210",
        "capture_mode": "game_capture",
    }
    display_entry = {
        "name": "Feishin",
        "path": "/app/main/feishin",
        "capture_mode": "display_capture",
    }
    app = make_status_application()
    app._config = ConfigStub(False, whitelist=[game_entry, display_entry])
    app._monitor_active_rules = {
        "steam-1782210": {"event": "process_started"},
        main_module.monitor_rule_id(display_entry, 1): {"event": "process_started"},
    }

    assert app._running_whitelist_entry("game_capture") == game_entry
    assert app._running_whitelist_entry("display_capture") == display_entry


def test_monitor_event_updates_active_rules_and_polls_status():
    app = make_status_application()
    poll_calls = []
    app._poll_engine_status = lambda: poll_calls.append(True) or True

    assert (
        app._on_monitor_event({"event": "process_started", "rule_id": "steam-730", "pid": 123})
        is False
    )
    assert app._monitor_active_rules == {
        "steam-730": {"event": "process_started", "rule_id": "steam-730", "pid": 123}
    }

    assert app._on_monitor_event({"event": "process_stopped", "rule_id": "steam-730"}) is False
    assert app._monitor_active_rules == {}
    assert poll_calls == [True, True]


def test_monitor_events_log_capture_start_and_stop():
    entry = {
        "name": "Counter-Strike 2",
        "appid": "730",
        "capture_mode": "display_capture",
    }
    app = make_status_application()
    app._config = ConfigStub(False, whitelist=[entry])
    app._poll_engine_status = lambda: True
    logs = []
    app._log = logs.append

    app._on_monitor_event({"event": "process_started", "rule_id": "steam-730", "pid": 7300})
    app._on_monitor_event({"event": "process_stopped", "rule_id": "steam-730"})

    assert logs == [
        "[clipper] Counter-Strike 2 detected, starting recording with display capture mode",
        "[clipper] Counter-Strike 2 is no longer running, stopping capture",
    ]


def test_stable_learned_game_audio_identity_is_persisted_and_restarts_engine():
    entry = {
        "name": "THE FINALS",
        "appid": "2073850",
        "install_path": "/games/The Finals",
        "capture_mode": "game_capture",
    }
    audio = {
        "tracks": [
            {
                "track": 1,
                "enabled": True,
                "sources": [
                    {
                        "kind": "game_app",
                        "display_name": "THE FINALS",
                        "match": {
                            "type": "process_name",
                            "value": "Discovery.exe",
                            "priority": "binary_first",
                        },
                        "learned_from": {"steam_appid": "2073850"},
                    }
                ],
            }
        ]
    }

    class LearningConfig:
        def __init__(self):
            self.values = {"whitelist": [entry], "audio": audio}
            self.saved = []

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value
            self.saved.append((key, value))

    engine_manager = EngineManagerStub(running=True)
    engine_client = EngineClientStub()
    app = make_status_application(engine_manager=engine_manager)
    app._config = LearningConfig()
    app._engine_client = engine_client
    app._monitor_active_rules = {
        "steam-2073850": {
            "event": "process_started",
            "rule_id": "steam-2073850",
            "pid": 2073,
        }
    }
    app._audio_learning_session_key = "2073850"
    app._audio_learning_candidate = ""
    app._audio_learning_candidate_count = 0
    app._audio_learning_resolved = False
    app._audio_learning_in_flight = True
    logs = []
    app._log = logs.append

    assert app._on_game_audio_learning_result("2073850", "Discovery-d.exe") is False
    assert app._config.saved == []

    app._audio_learning_in_flight = True
    assert app._on_game_audio_learning_result("2073850", "discovery-D.exe") is False

    assert [key for key, _value in app._config.saved] == ["whitelist", "audio"]
    assert app._config.values["whitelist"][0]["audio_process"] == "discovery-D.exe"
    assert (
        app._config.values["audio"]["tracks"][0]["sources"][0]["match"]["value"]
        == "discovery-D.exe"
    )
    assert engine_client.shutdown_callbacks[0]["restart"] is True
    assert logs == [
        (
            "[clipper] Audio probe for THE FINALS: candidate Discovery-d.exe "
            "matched by identity; confirming"
        ),
        "[clipper] Learned discovery-D.exe as the audio application for THE FINALS",
    ]


def test_monitor_stop_does_not_log_capture_stop_while_another_rule_is_active():
    entries = [
        {"name": "Counter-Strike 2", "appid": "730"},
        {"name": "Crab Game", "appid": "1782210"},
    ]
    app = make_status_application()
    app._config = ConfigStub(False, whitelist=entries)
    app._monitor_active_rules = {
        "steam-730": {"event": "process_started"},
        "steam-1782210": {"event": "process_started"},
    }
    app._poll_engine_status = lambda: True
    logs = []
    app._log = logs.append

    app._on_monitor_event({"event": "process_stopped", "rule_id": "steam-730"})

    assert logs == []


def test_obs_monitor_event_shows_visible_warning_once_without_becoming_game_rule():
    app = make_status_application()
    dialog_calls = []

    class VisibleWindow:
        def get_visible(self):
            return True

    app.window = VisibleWindow()
    app._show_obs_conflict_dialog = lambda: dialog_calls.append(True)
    app._poll_engine_status = lambda: (_ for _ in ()).throw(
        AssertionError("OBS events must not drive capture state")
    )

    event = {
        "event": "process_started",
        "rule_id": "clipper-observer-obs-studio",
        "pid": 410,
        "name": "OBS Studio",
    }
    assert app._on_monitor_event(event) is False
    assert app._on_monitor_event(event) is False

    assert app._obs_studio_running is True
    assert app._monitor_active_rules == {}
    assert dialog_calls == [True]


def test_obs_monitor_event_notifies_when_hidden_and_resets_after_exit():
    app = make_status_application()
    app.window = None
    notifications = []
    withdrawn = []
    app.send_notification = lambda notification_id, notification: notifications.append(
        (notification_id, notification.title, notification.body)
    )
    app.withdraw_notification = lambda notification_id: withdrawn.append(notification_id)

    started = {
        "event": "process_started",
        "rule_id": "clipper-observer-obs-studio",
        "pid": 410,
    }
    stopped = {
        "event": "process_stopped",
        "rule_id": "clipper-observer-obs-studio",
        "pid": 410,
    }

    app._on_monitor_event(started)
    app._on_monitor_event(started)
    app._on_monitor_event(stopped)
    app._on_monitor_event(started)

    assert [notification[0] for notification in notifications] == [
        "obs-studio-conflict",
        "obs-studio-conflict",
    ]
    assert notifications[0][1] == "OBS Studio and Clipper are both running"
    assert notifications[0][2] == (
        "Running both apps at the same time can overload the GPU or hardware encoder. "
        'This may also cause issues for games with "Game capture" method selected. '
        "It is advised to close OBS Studio or Clipper before recording."
    )
    assert withdrawn == ["obs-studio-conflict"]


def test_obs_warning_uses_red_acknowledge_response(monkeypatch):
    app = make_status_application()
    app.window = object()
    calls = []

    class AlertDialogStub:
        @classmethod
        def new(cls, title, body):
            calls.append(("new", title, body))
            return cls()

        def add_response(self, response_id, label):
            calls.append(("add", response_id, label))

        def set_response_appearance(self, response_id, appearance):
            calls.append(("appearance", response_id, appearance))

        def set_close_response(self, response_id):
            calls.append(("close", response_id))

        def set_default_response(self, response_id):
            calls.append(("default", response_id))

        def present(self, window):
            calls.append(("present", window))

    monkeypatch.setattr(main_module.Adw, "AlertDialog", AlertDialogStub)
    monkeypatch.setattr(
        main_module.Adw,
        "ResponseAppearance",
        types.SimpleNamespace(DESTRUCTIVE="destructive"),
        raising=False,
    )

    app._show_obs_conflict_dialog()

    assert ("add", "acknowledge", "Acknowledge") in calls
    assert ("appearance", "acknowledge", "destructive") in calls
    assert ("close", "acknowledge") in calls
    assert ("default", "acknowledge") in calls
    assert app._obs_conflict_dialog_shown is True


def test_language_change_saves_and_offers_foreground_restart(monkeypatch):
    calls = []

    class LanguageConfig:
        value = "system"

        def get(self, key, default=None):
            return self.value if key == "ui_language" else default

        def set(self, key, value):
            assert key == "ui_language"
            self.value = value
            calls.append(("saved", value))

    class AlertDialogStub:
        last = None

        def __init__(self, title, body):
            self.title = title
            self.body = body
            self.responses = {}
            self.callback = None
            AlertDialogStub.last = self

        @classmethod
        def new(cls, title, body):
            return cls(title, body)

        def add_response(self, response_id, label):
            self.responses[response_id] = label

        def set_default_response(self, response_id):
            self.default_response = response_id

        def set_close_response(self, response_id):
            self.close_response = response_id

        def set_response_appearance(self, response_id, appearance):
            self.appearance = (response_id, appearance)

        def connect(self, signal, callback):
            assert signal == "response"
            self.callback = callback

        def present(self, parent):
            self.parent = parent

    monkeypatch.setattr(main_module.Adw, "AlertDialog", AlertDialogStub)
    monkeypatch.setattr(
        main_module.Adw,
        "ResponseAppearance",
        types.SimpleNamespace(SUGGESTED="suggested"),
        raising=False,
    )
    app = object.__new__(ClipperApplication)
    app._config = LanguageConfig()
    app._restart_in_foreground = False
    app.quit = lambda: calls.append(("quit",))
    parent = object()

    app.request_language_change("en", parent)

    dialog = AlertDialogStub.last
    assert dialog is not None
    assert calls == [("saved", "en")]
    assert dialog.title == "Restart Clipper?"
    assert dialog.body == "Clipper needs to be restarted for this change to take effect."
    assert dialog.responses == {"later": "Restart later", "restart": "Restart now"}
    assert dialog.parent is parent

    assert dialog.callback is not None
    dialog.callback(dialog, "restart")

    assert app._restart_in_foreground is True
    assert calls[-1] == ("quit",)


def test_local_obs_detection_is_fallback_when_host_monitor_is_unavailable(monkeypatch):
    app = make_status_application()
    app._monitor_manager = MonitorManagerStub(available=False)
    observed = []
    app._set_obs_studio_running = lambda running: observed.append(running)
    monkeypatch.setattr(
        main_module,
        "find_running_obs_studio",
        lambda: {"pid": "410", "comm": "obs"},
    )

    app._poll_local_obs_studio()

    assert observed == [True]


def test_monitor_start_event_drives_display_capture_engine_start():
    entry = {
        "name": "Counter-Strike 2",
        "appid": "730",
        "capture_mode": "display_capture",
    }
    engine_manager = EngineManagerStub(running=False)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])

    assert (
        app._on_monitor_event({"event": "process_started", "rule_id": "steam-730", "pid": 7300})
        is False
    )

    assert engine_manager.start_calls == ["display_capture"]
    assert app._monitor_active_rules == {
        "steam-730": {"event": "process_started", "rule_id": "steam-730", "pid": 7300}
    }


def test_display_capture_status_uses_host_monitor_state_to_start_replay():
    entry = {
        "name": "Feishin",
        "path": "/app/main/feishin",
        "capture_mode": "display_capture",
    }
    engine_manager = EngineManagerStub(running=True)
    engine_manager.capture_mode = "display_capture"
    engine_client = EngineClientStub()
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])
    app._engine_client = engine_client
    rule_id = main_module.monitor_rule_id(entry, 0)
    app._monitor_active_rules = {
        rule_id: {"event": "process_started", "rule_id": rule_id, "pid": 7300}
    }

    app._on_status_response(
        {
            "ok": True,
            "buffer_active": False,
            "capture_mode": "display_capture",
        }
    )

    assert engine_client.start_replay_callbacks == [app._on_display_capture_sync_response]
    assert app._display_capture_sync_pending is True


def test_monitor_start_event_drives_game_capture_engine_start():
    entry = {
        "name": "Crab Game",
        "appid": "1782210",
        "capture_mode": "game_capture",
    }
    engine_manager = EngineManagerStub(running=False)
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])

    assert (
        app._on_monitor_event(
            {"event": "process_started", "rule_id": "steam-1782210", "pid": 1782210}
        )
        is False
    )

    assert engine_manager.start_calls == ["game_capture"]
    assert app._monitor_active_rules == {
        "steam-1782210": {
            "event": "process_started",
            "rule_id": "steam-1782210",
            "pid": 1782210,
        }
    }


def test_monitor_stop_event_shuts_down_idle_display_capture_engine():
    entry = {
        "name": "Counter-Strike 2",
        "appid": "730",
        "capture_mode": "display_capture",
    }
    engine_manager = EngineManagerStub(running=True)
    engine_client = EngineClientStub()
    engine_client.disconnected = True
    app = make_status_application(engine_manager=engine_manager)
    app._config = ConfigStub(False, whitelist=[entry])
    app._engine_client = engine_client
    app._monitor_active_rules = {
        "steam-730": {"event": "process_started", "rule_id": "steam-730", "pid": 7300}
    }
    app._last_running_entry_key = "730"

    assert app._on_monitor_event({"event": "process_stopped", "rule_id": "steam-730"}) is False

    assert engine_manager.terminated is True
    assert app._monitor_active_rules == {}
    assert app._last_running_entry_key is None


def test_monitor_ipc_start_automatically_starts_host_monitor(monkeypatch):
    app = make_status_application()
    manager = MonitorManagerStub()
    app._monitor_manager = manager
    created = {}

    class MonitorIpcStub:
        def __init__(self, callback, socket_path):
            created["callback"] = callback
            created["socket_path"] = socket_path

        def start(self):
            return True

    monkeypatch.setattr(main_module, "MonitorIpcServer", MonitorIpcStub)

    app._setup_monitor_ipc()

    assert manager.started is True
    assert created == {
        "callback": app._on_monitor_event,
        "socket_path": Path("/runtime/clipper/monitor.sock"),
    }


def test_monitor_does_not_start_when_ipc_server_fails(monkeypatch):
    app = make_status_application()
    manager = MonitorManagerStub()
    app._monitor_manager = manager

    class MonitorIpcStub:
        def __init__(self, _callback, socket_path):
            self.socket_path = socket_path

        def start(self):
            return False

    monkeypatch.setattr(main_module, "MonitorIpcServer", MonitorIpcStub)

    app._setup_monitor_ipc()

    assert manager.started is False
    assert app._monitor_ipc is None


def test_startup_diagnostics_report_session_system_and_existing_config(monkeypatch, capsys):
    app = make_status_application()
    app._config = types.SimpleNamespace(load_status="loaded", saved_key_count=17)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("FLATPAK_ID", "io.github.leesethefox.Clipper")
    monkeypatch.setattr(main_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(main_module.os, "cpu_count", lambda: 12)

    app._log_startup_diagnostics()
    app._log_startup_diagnostics()

    log = capsys.readouterr().out
    assert log.count("Session: wayland; desktop: KDE; runtime: Flatpak") == 1
    assert log.count("System: x86_64; 12 logical CPUs") == 1
    assert log.count("Local config: loaded existing config (17 saved keys)") == 1


def test_display_diagnostics_report_geometry_scale_and_refresh_once(capsys):
    class Geometry:
        width = 1080
        height = 1920

    class Monitor:
        def get_geometry(self):
            return Geometry()

        def get_scale_factor(self):
            return 2

        def get_scale(self):
            return 1920 / 1080

        def get_refresh_rate(self):
            return 144000

        def get_connector(self):
            return "HDMI-A-1"

    class Monitors:
        def get_n_items(self):
            return 1

        def get_item(self, _index):
            return Monitor()

    class Display:
        def get_monitors(self):
            return Monitors()

    app = make_status_application()
    app.window = types.SimpleNamespace(get_display=lambda: Display())
    app._display_diagnostics_logged = False

    app._log_display_diagnostics()
    app._log_display_diagnostics()

    log = capsys.readouterr().out
    assert log.count("Displays:") == 1
    assert (
        "HDMI-A-1 1080x1920 logical @ 144 Hz, rotated; scale unavailable (GTK rotation artifact)"
    ) in log
    assert "scale 2x" not in log
    assert "~2160x3840 pixels" not in log


def test_capability_probe_logs_request_and_completion(capsys):
    app = make_status_application()

    assert app.ensure_engine_for_capabilities() is True
    app.on_capabilities_finished()

    log = capsys.readouterr().out
    assert "OBS settings probe requested (temporary engine)" in log
    assert "OBS settings probe finished" in log
