from types import SimpleNamespace

import editor_window
from editor_window import EditorWindow
from gi.repository import Gdk


class FakeEditorWindow:
    _space_pressed = False

    def __init__(self):
        self.toggle_count = 0
        self.zoom_directions = []
        self.split_count = 0
        self.delete_count = 0
        self.select_all_count = 0
        self.seek_offsets = []
        self.close_request_count = 0
        self.mute_count = 0
        self.fullscreen_count = 0
        self.volume_directions = []
        self.project = SimpleNamespace(
            segments=[object(), object()],
            source=SimpleNamespace(frame_duration_us=41_667),
        )

    def _toggle_playback(self):
        self.toggle_count += 1

    def _change_timeline_zoom(self, direction, _anchor=None):
        self.zoom_directions.append(direction)

    def _split(self):
        self.split_count += 1

    def _delete(self):
        self.delete_count += 1

    def _select_all(self):
        self.select_all_count += 1

    def _seek_relative_to_playhead(self, offset_us):
        self.seek_offsets.append(offset_us)

    def _toggle_muted(self):
        self.mute_count += 1

    def _toggle_fullscreen(self):
        self.fullscreen_count += 1

    def _change_volume(self, direction):
        self.volume_directions.append(direction)

    def request_close(self):
        self.close_request_count += 1


def test_escape_defers_editor_close_until_key_event_is_consumed(monkeypatch):
    window = FakeEditorWindow()
    scheduled = []
    monkeypatch.setattr(
        editor_window.GLib,
        "idle_add",
        lambda callback, *args: scheduled.append((callback, args)) or 1,
    )

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Escape, 0, 0) is True
    assert window.close_request_count == 0
    assert len(scheduled) == 1

    callback, args = scheduled[0]
    callback(*args)

    assert window.close_request_count == 1


def test_escape_exits_video_fullscreen_without_closing_editor():
    window = FakeEditorWindow()
    exits = []
    window.is_fullscreen = lambda: True
    window.unfullscreen = lambda: exits.append(True)

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Escape, 0, 0) is True
    assert exits == [True]
    assert window.close_request_count == 0


def test_escape_closes_cancelled_export_dialog_without_closing_editor():
    window = FakeEditorWindow()
    closed = []
    window._exporter = None
    window._export_dialog = SimpleNamespace(close=lambda: closed.append(True))

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Escape, 0, 0) is True
    assert closed == [True]
    assert window.close_request_count == 0


def test_escape_during_active_export_keeps_dialog_and_editor_open():
    window = FakeEditorWindow()
    warnings = []
    window._exporter = object()
    window._export_dialog = SimpleNamespace(
        show_close_warning=lambda: warnings.append(True)
    )

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Escape, 0, 0) is True
    assert warnings == [True]
    assert window.close_request_count == 0


def test_modified_escape_does_not_request_editor_close():
    window = FakeEditorWindow()

    assert (
        EditorWindow._on_key_pressed(
            window,
            None,
            Gdk.KEY_Escape,
            0,
            Gdk.ModifierType.SHIFT_MASK,
        )
        is False
    )
    assert window.close_request_count == 0


def test_space_toggles_playback_and_is_consumed_before_the_focused_button():
    window = FakeEditorWindow()

    handled = EditorWindow._on_key_pressed(window, None, Gdk.KEY_space, 0, 0)
    repeated = EditorWindow._on_key_pressed(window, None, Gdk.KEY_space, 0, 0)

    assert handled is True
    assert repeated is True
    assert window.toggle_count == 1

    EditorWindow._on_key_released(window, None, Gdk.KEY_space, 0, 0)
    EditorWindow._on_key_pressed(window, None, Gdk.KEY_space, 0, 0)
    assert window.toggle_count == 2


def test_modified_space_and_other_keys_continue_to_their_normal_handlers():
    window = FakeEditorWindow()

    assert (
        EditorWindow._on_key_pressed(
            window,
            None,
            Gdk.KEY_space,
            0,
            Gdk.ModifierType.CONTROL_MASK,
        )
        is False
    )
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Return, 0, 0) is False
    assert window.toggle_count == 0


def test_ctrl_plus_and_minus_zoom_the_timeline_and_consume_the_shortcut():
    window = FakeEditorWindow()
    control = Gdk.ModifierType.CONTROL_MASK

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_plus, 0, control) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_equal, 0, control) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_KP_Add, 0, control) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_minus, 0, control) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_KP_Subtract, 0, control) is True

    assert window.zoom_directions == [1, 1, 1, -1, -1]


def test_ctrl_a_selects_every_segment_and_consumes_the_shortcut():
    window = FakeEditorWindow()

    assert (
        EditorWindow._on_key_pressed(
            window,
            None,
            Gdk.KEY_a,
            0,
            Gdk.ModifierType.CONTROL_MASK,
        )
        is True
    )

    assert window.select_all_count == 1


def test_zoom_shortcuts_do_not_override_alt_modified_keys():
    window = FakeEditorWindow()
    modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_plus, 0, modifiers) is False
    assert window.zoom_directions == []


def test_up_and_down_zoom_the_timeline_through_the_coalesced_zoom_path():
    window = FakeEditorWindow()

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Up, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Down, 0, 0) is True

    assert window.zoom_directions == [1, -1]


def test_up_and_down_adjust_volume_in_fullscreen_instead_of_zooming():
    window = FakeEditorWindow()
    window.is_fullscreen = lambda: True

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Up, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Down, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_KP_Up, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_KP_Down, 0, 0) is True

    assert window.volume_directions == [1, -1, 1, -1]
    assert window.zoom_directions == []


def test_edit_and_seek_shortcuts_are_consumed_and_use_editor_actions():
    window = FakeEditorWindow()

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_x, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Delete, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Left, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Right, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_comma, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_period, 0, 0) is True

    assert window.split_count == 1
    assert window.delete_count == 1
    assert window.seek_offsets == [-5_000_000, 5_000_000, -41_667, 41_667]


def test_fullscreen_blocks_split_and_delete_shortcut_actions():
    calls = []
    window = SimpleNamespace(
        is_fullscreen=lambda: True,
        history=SimpleNamespace(mutate=lambda *_args: calls.append("mutation")),
    )

    EditorWindow._split(window)
    EditorWindow._delete(window)

    assert calls == []


def test_fullscreen_blocks_undo_and_redo_actions():
    calls = []
    window = SimpleNamespace(
        is_fullscreen=lambda: True,
        history=SimpleNamespace(
            undo=lambda: calls.append("undo"),
            redo=lambda: calls.append("redo"),
        ),
        _refresh=lambda: calls.append("refresh"),
    )

    EditorWindow._undo(window)
    EditorWindow._redo(window)

    assert calls == []


def test_player_mute_and_fullscreen_shortcuts_work_in_the_editor():
    window = FakeEditorWindow()

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_m, 0, 0) is True
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_f, 0, 0) is True

    assert window.mute_count == 1
    assert window.fullscreen_count == 1


def test_letter_and_frame_shortcuts_use_their_physical_base_layout_keys(monkeypatch):
    class FakeDisplay:
        def map_keycode(self, keycode):
            keyvals = {
                41: [Gdk.KEY_Cyrillic_a, Gdk.KEY_f],
                58: [Gdk.KEY_Cyrillic_softsign, Gdk.KEY_m],
                53: [Gdk.KEY_Cyrillic_ha, Gdk.KEY_x],
                59: [Gdk.KEY_Cyrillic_be, Gdk.KEY_comma],
                60: [Gdk.KEY_Cyrillic_yu, Gdk.KEY_period],
            }[keycode]
            return True, [], keyvals

    monkeypatch.setattr(
        editor_window.Gdk.Display,
        "get_default",
        lambda: FakeDisplay(),
    )
    window = FakeEditorWindow()

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_a, 41, 0)
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_softsign, 58, 0)
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_ha, 53, 0)
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_be, 59, 0)
    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Cyrillic_yu, 60, 0)

    assert window.split_count == 1
    assert window.mute_count == 1
    assert window.fullscreen_count == 1
    assert window.seek_offsets == [-41_667, 41_667]


def test_delete_is_consumed_but_cannot_remove_the_last_segment():
    window = FakeEditorWindow()
    window.project.segments = [object()]

    assert EditorWindow._on_key_pressed(window, None, Gdk.KEY_Delete, 0, 0) is True
    assert window.delete_count == 0
