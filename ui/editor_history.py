"""Bounded snapshot command history for editor mutations."""

from __future__ import annotations

from dataclasses import dataclass

from editor_model import EditorProject

HISTORY_VERSION = 1


class HistoryValidationError(ValueError):
    pass


def _tuple_tree(value):
    if isinstance(value, list):
        return tuple(_tuple_tree(item) for item in value)
    return value


@dataclass
class SnapshotCommand:
    before: EditorProject
    after: EditorProject
    kind: str
    coalesce_key: tuple | None = None

    def apply(self) -> EditorProject:
        return self.after.clone()

    def reverse(self) -> EditorProject:
        return self.before.clone()

    def to_dict(self) -> dict:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "kind": self.kind,
            "coalesce_key": self.coalesce_key,
        }

    @classmethod
    def from_dict(cls, value: dict) -> SnapshotCommand:
        try:
            kind = value["kind"]
            if not isinstance(kind, str):
                raise TypeError("The command kind must be text")
            return cls(
                before=EditorProject.from_dict(value["before"]),
                after=EditorProject.from_dict(value["after"]),
                kind=kind,
                coalesce_key=_tuple_tree(value.get("coalesce_key")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HistoryValidationError(f"Invalid history command: {error}") from error


class EditorHistory:
    def __init__(self, project: EditorProject, limit: int = 200):
        self.project = project.clone()
        self.limit = max(1, limit)
        self.undo_stack: list[SnapshotCommand] = []
        self.redo_stack: list[SnapshotCommand] = []
        self.saved_digest = self.project.digest()
        self._live_command: SnapshotCommand | None = None

    def to_dict(self) -> dict:
        return {
            "version": HISTORY_VERSION,
            "undo_stack": [command.to_dict() for command in self.undo_stack],
            "redo_stack": [command.to_dict() for command in self.redo_stack],
        }

    @classmethod
    def from_dict(cls, project: EditorProject, value: dict, limit: int = 200) -> EditorHistory:
        try:
            if value["version"] != HISTORY_VERSION:
                raise HistoryValidationError(f"Unsupported history version {value['version']}")
            undo_values = value["undo_stack"]
            redo_values = value["redo_stack"]
            if not isinstance(undo_values, list) or not isinstance(redo_values, list):
                raise TypeError("History stacks must be lists")
            history = cls(project, limit)
            history.undo_stack = [
                SnapshotCommand.from_dict(command) for command in undo_values[-history.limit :]
            ]
            history.redo_stack = [
                SnapshotCommand.from_dict(command) for command in redo_values[-history.limit :]
            ]
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, HistoryValidationError):
                raise
            raise HistoryValidationError(f"Invalid editor history: {error}") from error

        for command in history.undo_stack + history.redo_stack:
            for snapshot in (command.before, command.after):
                if snapshot.project_id != project.project_id or snapshot.source != project.source:
                    raise HistoryValidationError(
                        "History does not belong to the saved editor project"
                    )
        return history

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    @property
    def dirty(self) -> bool:
        return self.project.digest() != self.saved_digest

    def mark_saved(self) -> None:
        self.saved_digest = self.project.digest()

    def mutate(self, kind: str, callback, *, coalesce_key=None) -> EditorProject:
        if self._live_command is not None:
            raise RuntimeError("A live editor mutation is already in progress")
        before = self.project.clone()
        callback(self.project)
        self.project.validate()
        after = self.project.clone()
        if before.digest() == after.digest():
            return self.project
        command = SnapshotCommand(before, after, kind, coalesce_key)
        self._record(command)
        return self.project

    def begin_live_mutation(self, kind: str, *, coalesce_key=None) -> None:
        """Capture one undo boundary for a high-frequency interactive edit."""
        if self._live_command is not None:
            raise RuntimeError("A live editor mutation is already in progress")
        before = self.project.clone()
        self._live_command = SnapshotCommand(before, before, kind, coalesce_key)

    def commit_live_mutation(self) -> EditorProject:
        if self._live_command is None:
            return self.project
        command = self._live_command
        self._live_command = None
        self.project.validate()
        command.after = self.project.clone()
        if command.before.digest() != command.after.digest():
            self._record(command)
        return self.project

    def _record(self, command: SnapshotCommand) -> None:
        if (
            command.coalesce_key is not None
            and self.undo_stack
            and self.undo_stack[-1].coalesce_key == command.coalesce_key
        ):
            command.before = self.undo_stack[-1].before
            self.undo_stack[-1] = command
        else:
            self.undo_stack.append(command)
            self.undo_stack = self.undo_stack[-self.limit :]
        self.redo_stack.clear()

    def undo(self) -> EditorProject:
        if not self.undo_stack:
            return self.project
        command = self.undo_stack.pop()
        self.redo_stack.append(command)
        self.project = command.reverse()
        return self.project

    def redo(self) -> EditorProject:
        if not self.redo_stack:
            return self.project
        command = self.redo_stack.pop()
        self.undo_stack.append(command)
        self.project = command.apply()
        return self.project
