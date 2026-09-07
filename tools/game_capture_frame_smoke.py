#!/usr/bin/env python3
"""Verify rendered game frames reach the installed Flatpak receiver.

Pass a compiled engine/gamecapture/frame_harness.c. Optional --steam-runtime
runs it through Steam's _v2-entry-point; --sandbox temporarily installs a local
test Flatpak and removes it and its data afterward. Neither mode edits Steam
data or Clipper's configuration. Requires locally installed Freedesktop 25.08
runtime/SDK and ffmpeg; nothing is downloaded by this test.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import game_capture


def check(harness: Path, api: str, runtime: Path | None, sandbox: bool) -> None:
    deployment = (
        Path(
            subprocess.check_output(
                ["flatpak", "--user", "info", "--show-location", game_capture.APP_ID], text=True
            ).strip()
        )
        / "files"
    )
    source = deployment / "share/clipper/game-capture/payloads" / game_capture.PAYLOAD_VERSION
    with (
        tempfile.TemporaryDirectory(prefix="clipper-frames-", dir="/var/tmp") as temporary,
        ExitStack() as cleanup,
    ):
        root = Path(temporary)
        wrapper = game_capture.ensure_payload(source, root / "hooks")
        socket_path = root / "engine.sock"
        command = [str(wrapper), str(harness), api]
        if runtime:
            command.insert(1, str(runtime))
            command.insert(2, "--")
        if sandbox:
            app = root / "sandbox"
            test_id = "io.github.leesethefox.ClipperCaptureTest." + root.name.replace("-", "_")
            subprocess.run(
                [
                    "flatpak",
                    "build-init",
                    str(app),
                    test_id,
                    "org.freedesktop.Sdk",
                    "org.freedesktop.Platform",
                    "25.08",
                ],
                check=True,
                capture_output=True,
            )
            permissions = [
                "--share=network",
                "--share=ipc",
                "--device=all",
                "--socket=x11",
                "--allow=devel",
                "--allow=multiarch",
                "--allow=per-app-dev-shm",
                f"--filesystem={root}",
                f"--filesystem={harness.parent}:ro",
                *([f"--filesystem={runtime.parent}"] if runtime else []),
            ]
            subprocess.run(
                ["flatpak", "build-finish", "--command=sh", *permissions, str(app)],
                check=True,
                capture_output=True,
            )
            repository = root / "repo"
            bundle = root / "test.flatpak"
            subprocess.run(
                ["flatpak", "build-export", str(repository), str(app)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["flatpak", "build-bundle", str(repository), str(bundle), test_id],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "flatpak",
                    "install",
                    "--user",
                    "--noninteractive",
                    "--no-deps",
                    "--no-related",
                    str(bundle),
                ],
                check=True,
                capture_output=True,
            )
            cleanup.callback(
                subprocess.run,
                ["flatpak", "uninstall", "--user", "--noninteractive", "--delete-data", test_id],
                check=True,
                capture_output=True,
            )
            command = [
                "flatpak",
                "run",
                "--user",
                "--die-with-parent",
                f"--command={command[0]}",
                test_id,
                *command[1:],
            ]
        # Keep the smoke engine independent of the user's normal configuration.
        config = root / "config"
        config.mkdir()
        with (root / "engine.log").open("w+") as elog, (root / "game.log").open("w+") as glog:
            engine = subprocess.Popen(
                [
                    "flatpak",
                    "run",
                    "--user",
                    "--die-with-parent",
                    f"--filesystem={root}",
                    "--command=env",
                    game_capture.APP_ID,
                    f"XDG_CONFIG_HOME={config}",
                    "/app/libexec/clipper/clipper-engine",
                    "--socket",
                    str(socket_path),
                    "--output-dir",
                    str(root),
                    "--capture-mode",
                    "game_capture",
                    "--verbose",
                ],
                stdout=elog,
                stderr=elog,
            )
            game = None
            try:
                deadline = time.monotonic() + 20
                while not socket_path.exists() and time.monotonic() < deadline:
                    if engine.poll() is not None:
                        raise RuntimeError("engine exited during startup")
                    time.sleep(0.1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(str(socket_path))
                    stream = client.makefile("rb")
                    game = subprocess.Popen(command, stdout=glog, stderr=glog)
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        client.sendall(b'{"cmd":"get_status"}\n')
                        while True:
                            message = json.loads(stream.readline())
                            if "game_hooked" in message:
                                break
                        if message["game_hooked"]:
                            client.sendall(b'{"cmd":"start_replay_buffer"}\n')
                            while "ok" not in json.loads(stream.readline()):
                                pass
                            time.sleep(1.5)
                            client.sendall(b'{"cmd":"save_replay_buffer"}\n')
                            clip = None
                            while clip is None:
                                event = json.loads(stream.readline())
                                if event.get("ok") is False:
                                    raise RuntimeError(f"replay save failed: {event}")
                                if event.get("event") == "clip_saved":
                                    clip = event["path"]
                            decoded = subprocess.check_output(
                                [
                                    "ffmpeg",
                                    "-v",
                                    "error",
                                    "-i",
                                    clip,
                                    "-map",
                                    "0:v:0",
                                    "-f",
                                    "framemd5",
                                    "-",
                                ],
                                text=True,
                                timeout=30,
                            )
                            frames = [
                                line.rsplit(",", 1)[-1]
                                for line in decoded.splitlines()
                                if line and not line.startswith("#")
                            ]
                            if len(set(frames)) < 2:
                                raise RuntimeError("capture has no changing video frames")
                            print(
                                f"PASS {api}: decoded {len(frames)} changing frames "
                                f"(sandbox={sandbox}, Steam runtime={bool(runtime)})"
                            )
                            return
                        if game.poll() is not None:
                            raise RuntimeError(f"game exited without connecting: {game.returncode}")
                        time.sleep(0.1)
                    raise RuntimeError("no game connection within 30 seconds")
            except Exception:
                elog.seek(0)
                glog.seek(0)
                print(elog.read()[-5000:], file=sys.stderr)
                print(glog.read()[-2500:], file=sys.stderr)
                raise
            finally:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as shutdown:
                        shutdown.settimeout(2)
                        shutdown.connect(str(socket_path))
                        shutdown.sendall(b'{"cmd":"shutdown"}\n')
                except OSError:
                    pass
                for process in (game, engine):
                    if process is not None:
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", type=Path)
    parser.add_argument("--api", choices=("opengl", "vulkan"), required=True)
    parser.add_argument("--steam-runtime", type=Path)
    parser.add_argument("--sandbox", action="store_true")
    args = parser.parse_args()
    check(args.harness.resolve(), args.api, args.steam_runtime, args.sandbox)
