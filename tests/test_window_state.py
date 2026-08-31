import json
from types import SimpleNamespace

from window_state import (
    WindowStateStore,
    clear_window_state,
    restored_size,
    select_monitor,
)


def _monitor(**overrides):
    details = {
        "connector": "DP-1",
        "manufacturer": "Example",
        "model": "Panel",
        "width": 1920,
        "height": 1080,
        "scale": 1,
    }
    details.update(overrides)
    return SimpleNamespace(), details


def test_restored_size_is_clamped_to_a_smaller_display():
    state = {"width": 2560, "height": 1440}
    current = _monitor(width=1280, height=720)[1]

    assert restored_size(state, current, (512, 500)) == (1280, 720)


def test_restored_size_respects_window_minimums():
    state = {"width": 200, "height": 100}

    assert restored_size(state, _monitor()[1], (512, 500)) == (512, 500)


def test_monitor_selection_survives_resolution_changes():
    monitors = [
        _monitor(connector="HDMI-1", model="Other"),
        _monitor(connector="DP-1", width=2560, height=1440),
    ]

    selected = select_monitor(
        {"connector": "DP-1", "width": 1920, "height": 1080},
        monitors,
    )

    assert selected is monitors[1]


def test_window_state_store_preserves_other_window_entries(tmp_path):
    store = WindowStateStore(tmp_path / "window-state.json")
    store.save("main", {"width": 800, "height": 600})
    store.save("player", {"width": 900, "height": 700})

    assert store.load("main") == {"width": 800, "height": 600}
    assert store.load("player") == {"width": 900, "height": 700}


def test_saved_window_state_contains_only_size_state(tmp_path):
    store = WindowStateStore(tmp_path / "window-state.json")
    state = {
        "width": 800,
        "height": 600,
        "maximized": False,
        "monitor": {"connector": "DP-1", "width": 1920, "height": 1080},
    }

    store.save("main", state)

    assert store.load("main") == state


def test_clearing_window_state_removes_saved_sizes(tmp_path):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "window-state.json"
    state_path.write_text(json.dumps({"main": {"width": 800}}), encoding="utf-8")

    clear_window_state(config_path)

    assert not state_path.exists()
