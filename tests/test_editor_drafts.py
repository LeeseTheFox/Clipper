import json

import pytest
from editor_drafts import (
    DraftError,
    StaleDraftError,
    draft_path,
    load_draft,
    load_draft_history,
    save_draft,
)
from editor_history import EditorHistory
from editor_model import EditorProject, Source


def test_atomic_save_load_and_stale_rejection(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    source = Source(str(tmp_path / "clip.mkv"), 1, 2, 10, 0)
    project = EditorProject.new(source)
    path = save_draft(project, env)
    assert path == draft_path(source.path, env)
    assert load_draft(source.path, source, env).digest() == project.digest()
    changed = Source(source.path, 2, 2, 10, 0)
    with pytest.raises(StaleDraftError):
        load_draft(source.path, changed, env)


def test_malformed_draft(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path)}
    path = draft_path("/clip.mkv", env)
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(DraftError):
        load_draft("/clip.mkv", env=env)


def test_saved_draft_restores_bounded_undo_and_redo_history(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    source = Source(str(tmp_path / "clip.mkv"), 1, 2, 10, 0)
    history = EditorHistory(EditorProject.new(source))
    segment = history.project.segments[0].id
    history.mutate("split", lambda project: project.split(segment, 5))
    history.undo()

    save_draft(history.project, env, history=history)
    restored = load_draft_history(source.path, source, env)

    assert restored is not None
    assert not restored.dirty
    assert restored.can_redo
    restored.redo()
    assert len(restored.project.segments) == 2
    restored.undo()
    assert len(restored.project.segments) == 1


def test_legacy_project_only_draft_loads_with_empty_history(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    source = Source(str(tmp_path / "clip.mkv"), 1, 2, 10, 0)
    project = EditorProject.new(source)
    path = draft_path(source.path, env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(project.to_dict()), encoding="utf-8")

    restored = load_draft_history(source.path, source, env)

    assert restored is not None
    assert restored.project.digest() == project.digest()
    assert not restored.can_undo
    assert not restored.can_redo


def test_invalid_saved_history_falls_back_to_the_current_edit(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data")}
    source = Source(str(tmp_path / "clip.mkv"), 1, 2, 10, 0)
    project = EditorProject.new(source)
    path = save_draft(project, env)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["history"]["undo_stack"] = [{"kind": "broken"}]
    path.write_text(json.dumps(value), encoding="utf-8")

    restored = load_draft_history(source.path, source, env)

    assert restored is not None
    assert restored.project.digest() == project.digest()
    assert not restored.can_undo
