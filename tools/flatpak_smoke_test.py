#!/usr/bin/env python3
"""Smoke-test the installed Clipper Flatpak prototype.

This intentionally validates the installed app, not the source checkout. It is
bounded and avoids a Flatpak rebuild.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

APP_ID = "io.github.leesethefox.Clipper"


def run(
    command: list[str],
    *,
    timeout: int = 120,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        input=input_text,
    )


def require_ok(label: str, completed: subprocess.CompletedProcess[str]) -> None:
    if completed.returncode == 0:
        print(f"PASS {label}")
        stdout = completed.stdout.strip()
        if stdout:
            for line in stdout.splitlines():
                print(f"  {line}")
        return

    print(f"FAIL {label}", file=sys.stderr)
    print(f"  command: {' '.join(completed.args)}", file=sys.stderr)
    print(f"  exit: {completed.returncode}", file=sys.stderr)
    if completed.stdout.strip():
        print("  stdout:", file=sys.stderr)
        print(textwrap.indent(completed.stdout.rstrip(), "    "), file=sys.stderr)
    if completed.stderr.strip():
        print("  stderr:", file=sys.stderr)
        print(textwrap.indent(completed.stderr.rstrip(), "    "), file=sys.stderr)
    raise SystemExit(completed.returncode or 1)


ENGINE_AND_MONITOR_SMOKE = r'''
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/app/share/clipper/ui")

from monitor_ipc import MonitorIpcServer
from monitor_manager import HostMonitorManager
import game_capture


def print_result(name, value):
    print(f"{name}={json.dumps(value, sort_keys=True)}")


def ui_import_smoke():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        import engine_manager
        import icon_names
        import main
        import monitor_ipc
        import monitor_manager
        import process_watcher
        import vdf
    except Exception as exc:
        print_result("ui_import", {"ok": False, "error": repr(exc)})
        return False

    ok = (
        icon_names.APP_ID == "io.github.leesethefox.Clipper"
        and hasattr(main, "ClipperApplication")
        and hasattr(engine_manager, "EngineProcessManager")
        and hasattr(monitor_ipc, "MonitorIpcServer")
        and hasattr(monitor_manager, "HostMonitorManager")
        and hasattr(process_watcher, "monitor_rule_id")
    )
    print_result("ui_import", {
        "ok": ok,
        "app_id": icon_names.APP_ID,
        "gtk_major": Gtk.get_major_version(),
        "adw_module": bool(Adw),
        "vdf_version": getattr(vdf, "__version__", "unknown"),
    })
    return ok


def read_ready(proc, timeout=35):
    assert proc.stdout and proc.stderr
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)
    stderr_lines = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, stderr_lines
        readable, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
        for stream in readable:
            try:
                line = stream.readline()
            except BlockingIOError:
                line = ""
            if not line:
                continue
            if stream is proc.stdout and line.startswith("READY "):
                return True, stderr_lines
            if stream is proc.stderr:
                stderr_lines.append(line.rstrip())
    return False, stderr_lines


def engine_save_smoke():
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "clipper" / f"smoke-{os.getpid()}"
    runtime.mkdir(parents=True, exist_ok=True)
    socket_path = runtime / "engine.sock"
    output_dir = runtime / "clips"
    output_dir.mkdir(exist_ok=True)

    proc = subprocess.Popen(
        [
            "/app/libexec/clipper/clipper-engine",
            "--socket",
            str(socket_path),
            "--output-dir",
            str(output_dir),
            "--max-time",
            "5",
            "--capture-mode",
            "display_capture",
            "--verbose",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready, stderr_lines = read_ready(proc)
    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print_result("engine_save", {
            "ok": False,
            "stage": "ready",
            "returncode": proc.returncode,
            "stderr_tail": stderr_lines[-20:],
        })
        return False

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(8)
    client.connect(str(socket_path))
    buf = b""

    def read_msg(timeout=8):
        nonlocal buf
        deadline = time.monotonic() + timeout
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([client], [], [], remaining)
            if not readable:
                return None
            chunk = client.recv(4096)
            if not chunk:
                return None
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def send(cmd):
        client.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
        while True:
            message = read_msg(10)
            if message is None:
                return None
            if "ok" in message:
                return message

    start = send("start_replay_buffer")
    if not start or not start.get("ok"):
        print_result("engine_save", {"ok": False, "stage": "start", "response": start})
        client.close()
        proc.terminate()
        return False

    time.sleep(3)
    save = send("save_replay_buffer")
    clip = None
    if save and save.get("ok"):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = read_msg(max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            if message.get("event") == "clip_saved":
                clip = message.get("path")
                break

    startup_log = "\n".join(stderr_lines)
    bundled_obs_ok = (
        "/app/lib/obs-plugins/obs-ffmpeg.so" in startup_log
        and "/app/lib/obs-plugins/obs-x264.so" in startup_log
        and "/app/lib/obs-plugins/obs-outputs.so" in startup_log
        and "com.obsproject.Studio" not in startup_log
    )
    ok = bool(save and save.get("ok") and clip and Path(clip).exists() and bundled_obs_ok)
    print_result("engine_save", {
        "ok": ok,
        "bundled_obs_ok": bundled_obs_ok,
        "clip": clip,
        "clip_exists": bool(clip and Path(clip).exists()),
        "output_dir": str(output_dir),
    })
    client.close()
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return ok


def monitor_smoke(isolated):
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "clipper" / f"monitor-smoke-{os.getpid()}"
    runtime.mkdir(parents=True, exist_ok=True)
    config_path = runtime / "config.json"
    proc_root = runtime / "proc"
    proc_root.mkdir(exist_ok=True)
    socket_path = runtime / "monitor.sock"
    events = []
    manager = HostMonitorManager(
        config_file=config_path,
        socket_path=socket_path,
        logger=lambda _message: None,
    )

    config_path.write_text(json.dumps({"whitelist": []}), encoding="utf-8")
    probe = manager.probe()
    probe_ok = probe.ok
    probe_stdout = probe.stdout.strip()
    probe_stderr = probe.stderr.strip()
    probe_returncode = probe.returncode

    if not probe_ok:
        print_result("monitor", {
            "ok": False,
            "stage": "probe",
            "returncode": probe_returncode,
            "stdout": probe_stdout,
            "stderr": probe_stderr,
            "isolated": isolated,
        })
        return False

    config_path.write_text(
        json.dumps({
            "whitelist": [{
                "name": "Smoke Game",
                "capture_mode": "display_capture",
                "path": "/usr/bin/smoke-game",
                "executable_path": "/usr/bin/smoke-game",
                "executable_name": "smoke-game",
            }]
        }),
        encoding="utf-8",
    )
    proc_dir = proc_root / "43210"
    proc_dir.mkdir(exist_ok=True)
    (proc_dir / "comm").write_text("smoke-game\n", encoding="utf-8")
    (proc_dir / "cmdline").write_bytes(b"/usr/bin/smoke-game\0")
    (proc_dir / "environ").write_bytes(b"")

    server = MonitorIpcServer(
        lambda event: events.append(event),
        socket_path=socket_path,
        dispatcher=lambda callback, args: callback(*args),
    )
    if not server.start():
        print_result("monitor", {"ok": False, "stage": "server_start"})
        return False

    command = manager.command_for("once")
    command.insert(-2, f"--env=CLIPPER_PROC_ROOT={proc_root}")
    once = subprocess.run(command, check=False, text=True, capture_output=True, timeout=10)
    time.sleep(0.2)
    server.stop()

    ok = (
        once.returncode == 0
        and any(event.get("event") == "process_started" for event in events)
    )
    print_result("monitor", {
        "ok": ok,
        "isolated": isolated,
        "probe_stdout": probe_stdout,
        "once_returncode": once.returncode,
        "once_stdout": once.stdout.strip(),
        "once_stderr": once.stderr.strip(),
        "events": events,
    })
    return ok


def game_capture_smoke():
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-capture-smoke-") as temp:
            wrapper = game_capture.ensure_payload(destination=Path(temp))
            result = subprocess.run(
                [str(wrapper), "true"], capture_output=True, text=True, timeout=10
            )
        ok = result.returncode == 0
        print_result("game_capture", {"ok": ok, "stderr": result.stderr})
        return ok
    except Exception as exc:
        print_result("game_capture", {"ok": False, "error": repr(exc)})
        return False


isolated = os.environ.get("CLIPPER_SMOKE_ISOLATED") == "1"
ui_ok = ui_import_smoke()
engine_ok = engine_save_smoke()
monitor_ok = monitor_smoke(isolated)
capture_ok = game_capture_smoke()
raise SystemExit(0 if ui_ok and engine_ok and monitor_ok and capture_ok else 1)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--isolated-data",
        action="store_true",
        help=(
            "run the installed Flatpak with temporary HOME/XDG data directories "
            "under $XDG_RUNTIME_DIR/clipper"
        ),
    )
    args = parser.parse_args()

    require_ok(
        "installed flatpak info",
        run(["flatpak", "--user", "info", APP_ID], timeout=20),
    )

    env_args = []

    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    script_dir = runtime_dir / "clipper"
    script_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.isolated_data:
        isolated_root = script_dir / f"flatpak-smoke-data-{os.getpid()}"
        isolated_home = isolated_root / "home"
        isolated_config = isolated_root / "config"
        isolated_data = isolated_root / "data"
        isolated_cache = isolated_root / "cache"
        for path in (isolated_home, isolated_config, isolated_data, isolated_cache):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        env_args.extend(
            [
                "--env=CLIPPER_SMOKE_ISOLATED=1",
                f"--env=HOME={isolated_home}",
                f"--env=XDG_CONFIG_HOME={isolated_config}",
                f"--env=XDG_DATA_HOME={isolated_data}",
                f"--env=XDG_CACHE_HOME={isolated_cache}",
            ]
        )
        print(f"INFO isolated data root: {isolated_root}")

    script_path = script_dir / f"flatpak-smoke-{os.getpid()}.py"
    script_path.write_text(ENGINE_AND_MONITOR_SMOKE, encoding="utf-8")
    try:
        require_ok(
            "installed engine save and host monitor IPC smoke",
            run(
                [
                    "flatpak",
                    "--user",
                    "run",
                    "--command=python3",
                    *env_args,
                    APP_ID,
                    str(script_path),
                ],
                timeout=90,
            ),
        )
    finally:
        try:
            script_path.unlink()
        except FileNotFoundError:
            pass
        run(["flatpak", "kill", APP_ID], timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
