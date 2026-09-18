#!/usr/bin/env python3
"""Check failed-save recovery with the installed Flatpak engine and real muxer.

Uses an isolated socket, configuration, and temporary output directory. Captures
the empty game scene, without connecting to a game or editing Clipper's settings.
"""

import argparse
import json
import socket
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path


def resource_snapshot(socket_path: Path) -> tuple[str, int]:
    """Locate only this test's engine; Flatpak's launcher PID is not the engine."""
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if (not argv[0].endswith(b"/clipper-engine")
                    or str(socket_path).encode() not in argv):
                continue
            rss = next(line.split(":", 1)[1].strip()
                       for line in (process / "status").read_text().splitlines()
                       if line.startswith("VmRSS:"))
            targets = []
            for descriptor in (process / "fd").iterdir():
                target = str(descriptor.readlink())
                targets.append(target.split(":", 1)[0]
                               if target.startswith(("pipe:", "socket:")) else target)
            return (f"rss={rss} fds={len(targets)} targets={dict(Counter(targets))}",
                    len(targets))
        except (OSError, StopIteration):
            continue
    raise RuntimeError("Could not inspect smoke engine resources")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", default="obs_x264")
    parser.add_argument("--resolution", default="320x180")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--vaapi-import-cache", action=argparse.BooleanOptionalAction)
    parser.add_argument("--vaapi-direct-surfaces", action=argparse.BooleanOptionalAction)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sample-resources", action="store_true")
    parser.add_argument("--assert-stable-fds", action="store_true",
                        help="Fail if stopped-engine FD counts grow after the first cycle")
    args = parser.parse_args()
    if args.assert_stable_fds and args.cycles < 2:
        parser.error("--assert-stable-fds requires at least two cycles")
    baseline_fds = None
    with tempfile.TemporaryDirectory(prefix="clipper-save-smoke-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        config = root / "config/clipper"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({
            "resolution": args.resolution, "fps": args.fps, "video_encoder": args.encoder,
            "replay_buffer_seconds": 2, "output_format": "mkv",
        }))
        output = root / "clips"
        output.mkdir()
        socket_path = root / "engine.sock"
        with (root / "engine.log").open("w+") as log:
            engine = subprocess.Popen([
                "flatpak", "run", "--user", "--die-with-parent", f"--filesystem={root}",
                "--command=env", "io.github.leesethefox.Clipper",
                "-u", "CLIPPER_VAAPI_IMPORT_CACHE", "-u", "CLIPPER_VAAPI_DIRECT_SURFACES",
                f"XDG_CONFIG_HOME={config.parent}",
                *([f"CLIPPER_VAAPI_IMPORT_CACHE={int(args.vaapi_import_cache)}"]
                  if args.vaapi_import_cache is not None else []),
                *([f"CLIPPER_VAAPI_DIRECT_SURFACES={int(args.vaapi_direct_surfaces)}"]
                  if args.vaapi_direct_surfaces is not None else []),
                "/app/libexec/clipper/clipper-engine",
                "--socket", str(socket_path), "--output-dir", str(output),
                "--capture-mode", "game_capture",
                "--verbose",
            ], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while not socket_path.exists():
                    if engine.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Smoke engine did not start")
                    time.sleep(0.1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(15)
                    client.connect(str(socket_path))
                    with client.makefile("rb") as stream:
                        def command(name):
                            client.sendall((json.dumps({"cmd": name}) + "\n").encode())

                        def receive():
                            line = stream.readline()
                            if not line:
                                raise RuntimeError("Engine disconnected")
                            return json.loads(line)

                        for cycle in range(args.cycles):
                            command("start_replay_buffer")
                            while True:
                                message = receive()
                                if "ok" in message:
                                    assert message["ok"], message
                                    break
                            time.sleep(1)
                            for writable in (False, False, True):
                                output.chmod(0o700 if writable else 0o500)
                                command("save_replay_buffer")
                                acknowledged = False
                                result = None
                                while not acknowledged or result is None:
                                    message = receive()
                                    if "ok" in message:
                                        assert message["ok"], message
                                        acknowledged = True
                                    if message.get("event") in ("clip_saved", "clip_save_failed"):
                                        result = message
                                expected = "clip_saved" if writable else "clip_save_failed"
                                assert result["event"] == expected, result
                                if writable:
                                    assert Path(result["path"]).stat().st_size > 0
                                    subprocess.run([
                                        "ffmpeg", "-v", "error", "-xerror", "-i", result["path"],
                                        "-f", "null", "-",
                                    ], check=True, capture_output=True, timeout=30)
                                print(f"PASS real replay writer: {expected}", flush=True)
                            command("stop_replay_buffer")
                            while True:
                                message = receive()
                                if "ok" in message:
                                    assert message["ok"], message
                                    break
                            print(f"PASS replay stop/restart cycle {cycle + 1}", flush=True)
                            if args.sample_resources or args.assert_stable_fds:
                                # OBS teardown can finish just after the IPC acknowledgment.
                                time.sleep(0.25)
                                snapshot, fds = resource_snapshot(socket_path)
                                if args.assert_stable_fds and baseline_fds is not None:
                                    deadline = time.monotonic() + 5
                                    while fds > baseline_fds and time.monotonic() < deadline:
                                        time.sleep(0.1)
                                        snapshot, fds = resource_snapshot(socket_path)
                                    if fds > baseline_fds:
                                        raise RuntimeError(
                                            f"FD growth after cycle {cycle + 1}: "
                                            f"baseline={baseline_fds}; {snapshot}"
                                        )
                                baseline_fds = fds if baseline_fds is None else baseline_fds
                                print(f"After stop: {snapshot}", flush=True)
                        command("shutdown")
                engine.wait(timeout=15)
                assert engine.returncode == 0, engine.returncode
                log.seek(0)
                for line in log:
                    if ("session=" in line or "Clipper VAAPI imports:" in line
                            or "Clipper VAAPI direct" in line):
                        print(line.strip())
            except Exception:
                log.seek(0)
                print(log.read()[-6000:])
                raise
            finally:
                output.chmod(0o700)
                if engine.poll() is None:
                    engine.terminate()
                    try:
                        engine.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        engine.kill()
                        engine.wait(timeout=5)


if __name__ == "__main__":
    main()
