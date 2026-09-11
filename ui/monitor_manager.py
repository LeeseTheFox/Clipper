"""Flatpak host-monitor lifecycle for automatic process detection.

Safe to import without GTK. The monitor starts with Clipper and is tied to the
app's Flatpak bus connection; it is not an optional persistent user service.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MONITOR_HELPER_RELATIVE = Path("libexec/clipper/clipper-monitor-host")
FLATPAK_INFO_PATH = Path("/.flatpak-info")
HOST_WORKING_DIRECTORY = "/"


@dataclass(frozen=True)
class MonitorCommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str


class MonitorManagerError(RuntimeError):
    """Raised when the host monitor cannot be addressed safely."""


class HostMonitorManager:
    """Start and stop Clipper's fixed host-monitor process."""

    _MODES = {
        "list-processes": "--list-processes",
        "probe": "--probe",
        "once": "--once",
        "service": "--service",
        "start-steam-native": "--start-steam-native",
        "stop-steam-native": "--stop-steam-native",
        "start-steam-flatpak": "--start-steam-flatpak",
        "stop-steam-flatpak": "--stop-steam-flatpak",
        "start-steam-snap": "--start-steam-snap",
        "stop-steam-snap": "--stop-steam-snap",
        "steam-running-native": "--steam-running-native",
        "steam-running-flatpak": "--steam-running-flatpak",
        "steam-running-snap": "--steam-running-snap",
    }

    _STEAM_VARIANTS = {
        "native_steam": "native",
        "flatpak_steam_user": "flatpak",
        "flatpak_steam_system": "flatpak",
        "steam_snap": "snap",
    }

    def __init__(
        self,
        *,
        flatpak_info_path: Path = FLATPAK_INFO_PATH,
        runner: Any = subprocess.run,
        launcher: Any = subprocess.Popen,
        env: dict[str, str] | None = None,
        config_file: Path | None = None,
        socket_path: Path | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._flatpak_info_path = flatpak_info_path
        self._runner = runner
        self._launcher = launcher
        self._env = env
        self._config_file = config_file
        self._socket_path = socket_path
        self._logger = logger or (lambda message: print(message, file=sys.stderr))
        self._process: Any | None = None

    def available(self) -> bool:
        env = self._env if self._env is not None else os.environ
        return bool(env.get("FLATPAK_ID")) and self._flatpak_info_path.exists()

    def probe(self) -> MonitorCommandResult:
        return self._run_operation("probe")

    def list_processes(self) -> list[dict[str, Any]] | None:
        """Return host process metadata, or None when the fixed operation fails."""
        if not self.available():
            return None

        result = self._run_operation("list-processes")
        if not result.ok:
            return None

        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            self._logger(f"[clipper] invalid host process list: {exc}")
            return None
        if not isinstance(payload, list):
            self._logger("[clipper] invalid host process list: expected an array")
            return None

        processes: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("pid") or "").strip()
            if not pid.isdigit():
                continue
            raw_rule_ids = item.get("rule_ids")
            rule_ids = (
                [rule_id for rule_id in raw_rule_ids if isinstance(rule_id, str) and rule_id]
                if isinstance(raw_rule_ids, list)
                else []
            )
            raw_namespace_pids = item.get("namespace_pids")
            namespace_pids = (
                [
                    str(namespace_pid)
                    for namespace_pid in raw_namespace_pids
                    if str(namespace_pid).isdigit() and int(namespace_pid) > 0
                ]
                if isinstance(raw_namespace_pids, list)
                else []
            )
            processes.append(
                {
                    "pid": pid,
                    "namespace_pids": namespace_pids,
                    "comm": str(item.get("comm") or ""),
                    "cmdline": str(item.get("cmdline") or ""),
                    "exe": str(item.get("exe") or ""),
                    "rule_ids": rule_ids,
                }
            )
            if item.get("flatpak_id"):
                processes[-1]["flatpak_id"] = str(item["flatpak_id"])
        return processes

    @classmethod
    def _steam_variant(cls, environment: object) -> str | None:
        value = getattr(environment, "value", environment)
        return cls._STEAM_VARIANTS.get(str(value))

    def steam_running(self, environment: object = "native_steam") -> bool | None:
        """Return whether one exact Steam packaging variant is running."""
        variant = self._steam_variant(environment)
        if variant is None:
            return None
        result = self._run_operation(f"steam-running-{variant}")
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def stop_steam(self, environment: object = "native_steam") -> MonitorCommandResult:
        """Ask the fixed host helper to close one exact Steam variant."""
        variant = self._steam_variant(environment)
        if variant is None:
            return MonitorCommandResult(False, 64, "", "Unsupported Steam environment")
        return self._run_operation(f"stop-steam-{variant}")

    def start_steam(self, environment: object = "native_steam") -> MonitorCommandResult:
        """Ask the fixed host helper to relaunch one exact Steam variant."""
        variant = self._steam_variant(environment)
        if variant is None:
            return MonitorCommandResult(False, 64, "", "Unsupported Steam environment")
        return self._run_operation(f"start-steam-{variant}")

    def start(self) -> bool:
        """Start automatic detection for the lifetime of this app process."""
        if not self.available():
            return False
        if self.is_running():
            return True

        try:
            command = self.command_for("service")
        except MonitorManagerError as exc:
            self._logger(f"[clipper] could not start host monitor: {exc}")
            return False

        self._log_operation("start", command)
        try:
            self._process = self._launcher(command, stdin=subprocess.DEVNULL)
        except OSError as exc:
            self._process = None
            self._logger(f"[clipper] could not start host monitor: {exc}")
            return False
        return True

    def stop(self) -> None:
        """Stop the app-scoped host monitor if it is running."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def command_for(self, mode: str) -> list[str]:
        if mode not in self._MODES:
            raise MonitorManagerError(f"Unsupported monitor mode: {mode}")

        helper = self._host_helper_path()
        command = [
            "flatpak-spawn",
            "--host",
        ]
        if mode == "service":
            command.append("--watch-bus")
        command.append(f"--directory={HOST_WORKING_DIRECTORY}")
        if self._config_file is not None:
            command.append(f"--env=CLIPPER_CONFIG_FILE={self._config_file}")
        if self._socket_path is not None:
            command.append(f"--env=CLIPPER_MONITOR_SOCKET={self._socket_path}")
        command.extend((str(helper), self._MODES[mode]))
        return command

    def _run_operation(self, operation: str) -> MonitorCommandResult:
        try:
            command = self.command_for(operation)
        except MonitorManagerError as exc:
            self._log_result(operation, 127)
            return MonitorCommandResult(False, 127, "", str(exc))
        self._log_operation(operation, command)
        timeout = 35 if operation.startswith(("start-steam-", "stop-steam-")) else 10
        try:
            completed = self._runner(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except OSError as exc:
            self._log_result(operation, 127)
            return MonitorCommandResult(False, 127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            self._log_result(operation, 124)
            stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout or ""
            )
            stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr or "host monitor command timed out"
            )
            return MonitorCommandResult(
                False,
                124,
                stdout,
                stderr,
            )

        self._log_result(operation, completed.returncode)
        return MonitorCommandResult(
            completed.returncode == 0,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def _host_helper_path(self) -> Path:
        app_files = _active_app_files_path(_read_flatpak_info(self._flatpak_info_path))
        return app_files / MONITOR_HELPER_RELATIVE

    def _log_operation(self, operation: str, command: list[str]) -> None:
        self._logger(f"[clipper] host monitor operation {operation}: {shlex.join(command)}")

    def _log_result(self, operation: str, returncode: int) -> None:
        self._logger(
            f"[clipper] host monitor operation {operation} finished with exit code {returncode}"
        )


def _read_flatpak_info(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MonitorManagerError(f"Could not read {path}: {exc}") from exc

    for line in lines:
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _active_app_files_path(flatpak_info: dict[str, str]) -> Path:
    raw_app_path = flatpak_info.get("app-path", "")
    if not raw_app_path:
        raise MonitorManagerError("Missing app-path in /.flatpak-info")

    app_path = Path(raw_app_path)
    if app_path.name == "files":
        deployment = app_path.parent
        if deployment.name == "active":
            return app_path
        return deployment.parent / "active" / "files"

    if app_path.name == "active":
        return app_path / "files"

    return app_path.parent / "active" / "files"
