from types import SimpleNamespace

import editor_drafts
import editor_wipe
from editor_history import EditorHistory
from editor_model import EditorProject, Source
from editor_window import EditorWindow


class PreviewStub:
    def __init__(self, calls):
        self.calls = calls

    def pause(self):
        self.calls.append("pause")

    def seek_output(self, output_us):
        self.calls.append(("seek", output_us))

    def finish_seek(self):
        self.calls.append("finish seek")


class WindowStub:
    def __init__(self, history):
        self.history = history
        self.selected_segment_id = history.project.segments[-1].id
        self.selected_segment_ids = {self.selected_segment_id}
        self._selection_anchor_id = self.selected_segment_id
        self._playhead_us = 3_000_000
        self._zoom = 2.0
        self._scrubbing = True
        self._transport_seek_dragging = True
        self._gain_drag_key = ("gain", "a0")
        self._gain_lane_drag = object()
        self._gain_lane_click_suppressed = True
        self._zoom_anchor_timeout_id = None
        self._zoom_dragging = True
        self._zoom_discrete_active = True
        self._pending_zoom_anchor = object()
        self._pending_zoom_fraction = 0.5
        self._next_zoom_anchor = object()
        self._pending_scroll_value = None
        self._playback_follow_active = True
        self._playback_follow_suspended = True
        self._playback_follow_visible_x = 42
        self._viewport_refresh_pending = True
        self._refresh_after_scrub = True
        self.calls = []
        self.preview = PreviewStub(self.calls)

    @property
    def project(self):
        return self.history.project

    @property
    def dirty(self):
        return self.history.dirty

    def _cancel_scrub_preview_seek(self):
        self.calls.append("cancel scrub")

    def _stop_edge_scroll(self):
        self.calls.append("stop edge scroll")

    def _cancel_gain_redraw(self):
        self.calls.append("cancel gain redraw")

    def _cancel_zoom_update(self):
        self.calls.append("cancel zoom")

    def _cancel_discrete_zoom_settle(self):
        self.calls.append("cancel zoom settle")

    def _cancel_waveform_detail_restore(self):
        self.calls.append("cancel waveform restore")

    def _refresh(self):
        self.calls.append("refresh")

    def _reset_to_unedited_project(self, history):
        EditorWindow._reset_to_unedited_project(self, history)


def _edited_history(path="/clips/Example.mkv"):
    project = EditorProject.new(
        Source(
            path=path,
            size=100,
            mtime_ns=200,
            duration_us=8_000_000,
            video_stream_index=0,
        )
    )
    history = EditorHistory(project)
    history.mutate(
        "split",
        lambda current: current.split(current.segments[0].id, 4_000_000),
    )
    history.mark_saved()
    history.mutate("delete", lambda current: current.delete(current.segments[0].id))
    return history


def test_editor_wipe_confirmation_uses_the_shared_clips_prompt(monkeypatch):
    calls = []
    window = SimpleNamespace(
        project=SimpleNamespace(source=SimpleNamespace(path="/clips/Example.mkv")),
        _block_close_during_export=lambda: False,
        _on_wipe_edits_confirmed=object(),
    )
    monkeypatch.setattr(
        editor_wipe,
        "present_wipe_edits_confirmation",
        lambda *args: calls.append(args),
    )

    EditorWindow._confirm_wipe_edits(window)

    assert calls[0][1:] == (
        window,
        "Example.mkv",
        window._on_wipe_edits_confirmed,
    )


def test_confirming_editor_wipe_deletes_draft_and_resets_all_in_memory_edits(
    monkeypatch,
):
    window = WindowStub(_edited_history())
    deleted = []
    monkeypatch.setattr(
        editor_drafts,
        "delete_editor_data",
        lambda path: deleted.append(path) or True,
    )

    EditorWindow._on_wipe_edits_confirmed(window, None, "wipe")

    assert deleted == ["/clips/Example.mkv"]
    assert len(window.project.segments) == 1
    assert window.project.segments[0].source_start_us == 0
    assert window.project.segments[0].source_end_us == 8_000_000
    assert window.history.can_undo is False
    assert window.history.can_redo is False
    assert window.dirty is False
    assert window.selected_segment_ids == {window.project.segments[0].id}
    assert window._playhead_us == 0
    assert window._zoom == 1.0
    assert window._pending_scroll_value == 0
    assert window.calls[-3:] == ["refresh", ("seek", 0), "finish seek"]


def test_cancelling_editor_wipe_preserves_saved_and_unsaved_edits(monkeypatch):
    window = WindowStub(_edited_history())
    original_digest = window.project.digest()
    deleted = []
    monkeypatch.setattr(editor_drafts, "delete_editor_data", deleted.append)

    EditorWindow._on_wipe_edits_confirmed(window, None, "cancel")

    assert deleted == []
    assert window.project.digest() == original_digest
