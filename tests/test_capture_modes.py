from capture_modes import (
    CAPTURE_MODE_DISPLAY,
    CAPTURE_MODE_GAME,
    capture_mode_for_entry,
    capture_mode_label,
    with_capture_mode,
)


def test_missing_capture_mode_defaults_to_display_capture():
    assert capture_mode_for_entry({"name": "Game"}) == CAPTURE_MODE_DISPLAY


def test_legacy_method_labels_are_supported():
    assert capture_mode_for_entry({"method": "Game Capture"}) == CAPTURE_MODE_GAME
    assert capture_mode_for_entry({"method": "PipeWire"}) == CAPTURE_MODE_DISPLAY


def test_capture_mode_label_marks_game_capture_advanced():
    assert capture_mode_label(CAPTURE_MODE_DISPLAY) == "Display capture"
    assert capture_mode_label(CAPTURE_MODE_GAME) == "Game capture"


def test_with_capture_mode_stores_stable_mode_without_display_label():
    entry = with_capture_mode({"name": "Game"}, CAPTURE_MODE_GAME)

    assert entry["capture_mode"] == CAPTURE_MODE_GAME
    assert "method" not in entry
