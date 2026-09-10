"""Atomic XDG draft persistence and editor cache paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from editor_history import EditorHistory, HistoryValidationError
from editor_model import EditorProject, ProjectValidationError, Source

DRAFT_VERSION = 2


class DraftError(RuntimeError):
    pass


class StaleDraftError(DraftError):
    pass


def data_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def cache_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def source_key(path: str | Path) -> str:
    canonical = str(Path(path).expanduser().resolve())
    return hashlib.sha256(canonical.encode()).hexdigest()


def source_fingerprint(source: Source) -> str:
    value = {
        "path": str(Path(source.path).resolve()),
        "size": source.size,
        "mtime_ns": source.mtime_ns,
        "duration_us": source.duration_us,
        "video": source.video_stream_index,
        "audio": source.stream_signature(),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def draft_path(path: str | Path, env=None) -> Path:
    return data_home(env) / "clipper/editor/drafts" / f"{source_key(path)}.json"


def cache_path(source: Source, env=None) -> Path:
    return cache_home(env) / "clipper/editor" / source_fingerprint(source)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_draft(project: EditorProject, env=None, *, history: EditorHistory | None = None) -> Path:
    project.validate()
    if history is None:
        history = EditorHistory(project)
    elif history.project.digest() != project.digest():
        raise DraftError("The editor history does not match the saved project")
    path = draft_path(project.source.path, env)
    _atomic_json(
        path,
        {
            "draft_version": DRAFT_VERSION,
            "project": project.to_dict(),
            "history": history.to_dict(),
        },
    )
    return path


def source_matches(saved: Source, current: Source) -> bool:
    return (
        Path(saved.path).resolve() == Path(current.path).resolve()
        and saved.size == current.size
        and saved.mtime_ns == current.mtime_ns
        and saved.duration_us == current.duration_us
        and saved.video_stream_index == current.video_stream_index
        and saved.stream_signature() == current.stream_signature()
    )


def load_draft_history(
    path: str | Path, current_source: Source | None = None, env=None
) -> EditorHistory | None:
    location = draft_path(path, env)
    if not location.exists():
        return None
    try:
        with location.open(encoding="utf-8") as source:
            raw = json.load(source)
        if "draft_version" not in raw:
            project = EditorProject.from_dict(raw)
            history = EditorHistory(project)
        else:
            if raw["draft_version"] != DRAFT_VERSION:
                raise DraftError(f"Unsupported editor draft version {raw['draft_version']}")
            project = EditorProject.from_dict(raw["project"])
            try:
                history = EditorHistory.from_dict(project, raw["history"])
            except (HistoryValidationError, KeyError, TypeError):
                history = EditorHistory(project)
    except (OSError, json.JSONDecodeError, ProjectValidationError) as error:
        raise DraftError(f"Could not load the saved edit: {error}") from error
    if current_source is not None and not source_matches(project.source, current_source):
        raise StaleDraftError("The source clip changed since this edit was saved")
    return history


def load_draft(
    path: str | Path, current_source: Source | None = None, env=None
) -> EditorProject | None:
    history = load_draft_history(path, current_source, env)
    return None if history is None else history.project


def load_or_create(source: Source, env=None) -> EditorProject:
    return load_draft(source.path, source, env) or EditorProject.new(source)


def load_or_create_history(source: Source, env=None) -> EditorHistory:
    return load_draft_history(source.path, source, env) or EditorHistory(EditorProject.new(source))


def rename_editor_data(old_path: str | Path, new_path: str | Path, env=None) -> bool:
    """Move a saved editor draft when its source clip is renamed."""
    old_location = draft_path(old_path, env)
    if not old_location.exists():
        return True

    try:
        with old_location.open(encoding="utf-8") as source_file:
            raw: Any = json.load(source_file)
        project_data: Any = raw.get("project", raw) if isinstance(raw, dict) else None
        source_data: Any = project_data.get("source") if isinstance(project_data, dict) else None
        if not isinstance(source_data, dict):
            return False
        source_data = dict(source_data)
        source_data["path"] = str(Path(new_path))
        project_data = dict(project_data)
        project_data["source"] = source_data
        updated: dict[str, Any] = dict(raw)
        if "project" in updated:
            updated["project"] = project_data
        else:
            updated = project_data
        _atomic_json(draft_path(new_path, env), updated)
        old_location.unlink()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False

    # Render caches are keyed by the source fingerprint, which includes its
    # path. Move the cache when the draft provided enough source information.
    try:
        old_project = EditorProject.from_dict(raw.get("project", raw))
        new_project = EditorProject.from_dict(project_data)
        old_cache = cache_path(old_project.source, env)
        new_cache = cache_path(new_project.source, env)
        if old_cache.exists() and not new_cache.exists():
            new_cache.parent.mkdir(parents=True, exist_ok=True)
            old_cache.rename(new_cache)
    except (OSError, KeyError, TypeError, ValueError, ProjectValidationError):
        pass
    return True


def delete_editor_data(path: str | Path, source: Source | None = None, env=None) -> bool:
    deleted = True
    try:
        draft_path(path, env).unlink(missing_ok=True)
    except OSError:
        deleted = False
    if source is not None:
        shutil.rmtree(cache_path(source, env), ignore_errors=True)
    return deleted
