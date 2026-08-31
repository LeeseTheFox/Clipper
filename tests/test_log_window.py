import importlib.util
import sys
import types
from pathlib import Path


def _load_log_window_module():
    previous = {name: sys.modules.get(name) for name in ("gi", "gi.repository", "log_window")}
    removed_sources = []
    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace(Window=type("Window", (), {}))
    repository.GLib = types.SimpleNamespace(
        SOURCE_REMOVE=False,
        source_remove=lambda source_id: removed_sources.append(source_id),
    )
    repository.Gtk = types.SimpleNamespace()
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    gi.repository = repository
    sys.modules.update({"gi": gi, "gi.repository": repository})

    path = Path(__file__).parents[1] / "ui" / "log_window.py"
    spec = importlib.util.spec_from_file_location("log_window", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["log_window"] = module
    spec.loader.exec_module(module)

    return module, removed_sources, previous


def _restore_modules(previous):
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_close_request_stops_live_refresh_and_explicitly_destroys_window():
    module, removed_sources, previous = _load_log_window_module()
    try:
        window = object.__new__(module.LogWindow)
        unsubscribed = []
        destroyed = []
        window._destroyed = False
        window._unsubscribe = lambda: unsubscribed.append(True)
        window._idle_source_id = 42
        window.destroy = lambda: destroyed.append(True)

        assert window._on_close_request() is True
        assert window._destroyed is True
        assert unsubscribed == [True]
        assert removed_sources == [42]
        assert window._idle_source_id is None
        assert destroyed == [True]

        # The later destroy signal must not unsubscribe or remove twice.
        window._on_destroy()
        assert unsubscribed == [True]
        assert removed_sources == [42]
    finally:
        _restore_modules(previous)


def test_log_notifications_are_ignored_once_closing_has_started():
    module, _removed_sources, previous = _load_log_window_module()
    try:
        window = object.__new__(module.LogWindow)
        window._destroyed = True
        window._update_pending = False

        window._on_logs_changed()

        assert window._update_pending is False
    finally:
        _restore_modules(previous)
