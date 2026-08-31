from editor_history import EditorHistory
from editor_model import AudioTrack, EditorProject, Source


def project():
    return EditorProject.new(
        Source(
            "/clip.mkv",
            1,
            2,
            1_000_000,
            0,
            [AudioTrack("a0", 0, 1, "Track 1")],
            variable_frame_rate=True,
        )
    )


def test_undo_redo_branch_and_saved_digest():
    history = EditorHistory(project())
    segment = history.project.segments[0].id
    history.mutate("gain", lambda item: item.set_gain(segment, "a0", 0.5))
    assert history.dirty and history.can_undo
    history.undo()
    assert not history.dirty and history.can_redo
    history.redo()
    assert history.project.segments[0].audio["a0"].gain == 0.5
    history.mark_saved()
    assert not history.dirty
    history.undo()
    history.mutate("mute", lambda item: item.set_muted(segment, "a0", True))
    assert not history.can_redo


def test_slider_drag_coalesces():
    history = EditorHistory(project())
    segment = history.project.segments[0].id
    for gain in (0.8, 0.7, 0.6):
        history.mutate(
            "gain",
            lambda item, gain=gain: item.set_gain(segment, "a0", gain),
            coalesce_key=("gain", segment, "a0"),
        )
    assert len(history.undo_stack) == 1
    history.undo()
    assert history.project.segments[0].audio["a0"].gain == 1


def test_live_slider_drag_records_only_the_finished_state():
    history = EditorHistory(project())
    segment = history.project.segments[0].id

    history.begin_live_mutation(
        "gain",
        coalesce_key=("gain", segment, "a0"),
    )
    for gain in (0.8, 0.7, 0.6):
        history.project.set_gain(segment, "a0", gain)

    assert history.undo_stack == []
    history.commit_live_mutation()

    assert len(history.undo_stack) == 1
    assert history.project.segments[0].audio["a0"].gain == 0.6
    history.undo()
    assert history.project.segments[0].audio["a0"].gain == 1


def test_serialized_history_restores_undo_redo_and_coalescing_keys():
    history = EditorHistory(project())
    segment = history.project.segments[0].id
    history.mutate(
        "gain",
        lambda item: item.set_gain(segment, "a0", 0.5),
        coalesce_key=("gain", (segment,), "a0"),
    )
    history.mutate("mute", lambda item: item.set_muted(segment, "a0", True))
    history.undo()

    restored = EditorHistory.from_dict(history.project, history.to_dict())

    assert not restored.dirty
    assert restored.can_undo
    assert restored.can_redo
    assert restored.undo_stack[-1].coalesce_key == ("gain", (segment,), "a0")
    restored.redo()
    assert restored.project.segments[0].audio["a0"].muted is True
    restored.undo()
    restored.undo()
    assert restored.project.segments[0].audio["a0"].gain == 1


def test_restored_history_remains_bounded():
    history = EditorHistory(project(), limit=3)
    segment = history.project.segments[0].id
    for index in range(5):
        history.mutate(
            f"gain-{index}",
            lambda item, gain=(index + 1) / 10: item.set_gain(segment, "a0", gain),
        )

    restored = EditorHistory.from_dict(history.project, history.to_dict(), limit=2)

    assert len(restored.undo_stack) == 2
