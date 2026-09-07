from pathlib import Path

import pytest
from monitor_manager import HostMonitorManager, MonitorManagerError


def _flatpak_info(tmp_path: Path, app_path: str) -> Path:
    info = tmp_path / "flatpak-info"
    info.write_text(
        "\n".join(
            [
                "[Application]",
                "name=io.github.leesethefox.Clipper",
                f"app-path={app_path}",
                "app-commit=abcdef",
            ]
        ),
        encoding="utf-8",
    )
    return info


def test_probe_command_uses_active_files_helper_for_commit_deployment(tmp_path):
    info = _flatpak_info(
        tmp_path,
        "/var/lib/flatpak/app/io.github.leesethefox.Clipper/x86_64/stable/abcdef/files",
    )
    manager = HostMonitorManager(flatpak_info_path=info)

    assert manager.command_for("probe") == [
        "flatpak-spawn",
        "--host",
        "--directory=/",
        "/var/lib/flatpak/app/io.github.leesethefox.Clipper/x86_64/stable/active/files/libexec/clipper/clipper-monitor-host",
        "--probe",
    ]


def test_service_command_is_app_scoped_and_passes_monitor_paths(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/active/files")
    manager = HostMonitorManager(
        flatpak_info_path=info,
        config_file=Path("/sandbox/config/clipper/config.json"),
        socket_path=Path("/runtime/clipper/monitor.sock"),
    )

    assert manager.command_for("service") == [
        "flatpak-spawn",
        "--host",
        "--watch-bus",
        "--directory=/",
        "--env=CLIPPER_CONFIG_FILE=/sandbox/config/clipper/config.json",
        "--env=CLIPPER_MONITOR_SOCKET=/runtime/clipper/monitor.sock",
        "/deploy/active/files/libexec/clipper/clipper-monitor-host",
        "--service",
    ]


def test_removed_service_management_operation_is_rejected(tmp_path):
    manager = HostMonitorManager(flatpak_info_path=_flatpak_info(tmp_path, "/deploy/files"))

    with pytest.raises(MonitorManagerError):
        manager.command_for("remove")


def test_available_requires_flatpak_id_and_info_file(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/files")

    assert HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
    ).available()
    assert not HostMonitorManager(flatpak_info_path=info, env={}).available()
    assert not HostMonitorManager(
        flatpak_info_path=tmp_path / "missing",
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
    ).available()


def test_probe_runner_receives_fixed_command(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/abcdef/files")
    calls = []
    logs = []

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def runner(cmd, check, text, capture_output, timeout):
        calls.append((cmd, check, text, capture_output, timeout))
        return Completed()

    result = HostMonitorManager(
        flatpak_info_path=info,
        runner=runner,
        logger=logs.append,
    ).probe()

    assert result.ok
    assert calls[0] == (
        [
            "flatpak-spawn",
            "--host",
            "--directory=/",
            "/deploy/active/files/libexec/clipper/clipper-monitor-host",
            "--probe",
        ],
        False,
        True,
        True,
        10,
    )
    assert logs[-1] == ("[clipper] host monitor operation probe finished with exit code 0")


def test_list_processes_uses_fixed_host_operation_and_validates_payload(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/abcdef/files")
    calls = []

    class Completed:
        returncode = 0
        stdout = (
            '[{"pid":42,"comm":"game","cmdline":"/games/game --play",'
            '"exe":"/games/game"},{"pid":"bad","comm":"ignored"}]'
        )
        stderr = ""

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Completed()

    processes = HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
        runner=runner,
        logger=lambda _message: None,
    ).list_processes()

    assert processes == [
        {
            "pid": "42",
            "namespace_pids": [],
            "comm": "game",
            "cmdline": "/games/game --play",
            "exe": "/games/game",
            "rule_ids": [],
        }
    ]
    assert calls[0][0][-1] == "--list-processes"


def test_list_processes_preserves_valid_matching_rule_ids(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/abcdef/files")

    class Completed:
        returncode = 0
        stdout = (
            '[{"pid":42,"comm":"Discovery-d.exe","cmdline":"game",'
            '"exe":"/usr/bin/wine64","namespace_pids":[42,930,0,"bad"],'
            '"rule_ids":["steam-2073850","",7]}]'
        )
        stderr = ""

    processes = HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
        runner=lambda _cmd, **_kwargs: Completed(),
        logger=lambda _message: None,
    ).list_processes()

    assert processes == [
        {
            "pid": "42",
            "namespace_pids": ["42", "930"],
            "comm": "Discovery-d.exe",
            "cmdline": "game",
            "exe": "/usr/bin/wine64",
            "rule_ids": ["steam-2073850"],
        }
    ]


def test_list_processes_rejects_invalid_json(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/active/files")

    class Completed:
        returncode = 0
        stdout = "not-json"
        stderr = ""

    manager = HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
        runner=lambda _cmd, **_kwargs: Completed(),
        logger=lambda _message: None,
    )

    assert manager.list_processes() is None


def test_steam_lifecycle_uses_only_fixed_helper_operations(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/active/files")
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Completed()

    manager = HostMonitorManager(
        flatpak_info_path=info,
        runner=runner,
        logger=lambda _message: None,
    )

    assert manager.stop_steam().ok
    assert manager.start_steam().ok
    assert [command[-1] for command, _kwargs in calls] == [
        "--stop-steam-native",
        "--start-steam-native",
    ]
    assert [kwargs["timeout"] for _command, kwargs in calls] == [35, 35]


def test_start_launches_one_app_scoped_process_and_stop_terminates_it(tmp_path):
    info = _flatpak_info(tmp_path, "/deploy/active/files")
    launches = []

    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            self.returncode = 0
            return 0

    process = Process()

    def launcher(command, stdin):
        launches.append((command, stdin))
        return process

    manager = HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
        launcher=launcher,
        logger=lambda _message: None,
    )

    assert manager.start() is True
    assert manager.start() is True
    assert manager.is_running() is True
    assert len(launches) == 1
    assert "--watch-bus" in launches[0][0]

    manager.stop()

    assert process.terminated is True
    assert process.wait_timeouts == [3]
    assert manager.is_running() is False


def test_start_is_disabled_outside_flatpak(tmp_path):
    manager = HostMonitorManager(
        flatpak_info_path=_flatpak_info(tmp_path, "/deploy/files"),
        env={},
    )

    assert manager.start() is False


def test_operation_reports_invalid_flatpak_info_as_a_result(tmp_path):
    info = _flatpak_info(tmp_path, "")
    logs = []

    result = HostMonitorManager(
        flatpak_info_path=info,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
        logger=logs.append,
    ).probe()

    assert not result.ok
    assert result.returncode == 127
    assert "Missing app-path" in result.stderr
    assert logs == ["[clipper] host monitor operation probe finished with exit code 127"]
