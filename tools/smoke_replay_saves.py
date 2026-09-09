#!/usr/bin/env python3
"""Check failed-save recovery with the installed Flatpak engine and real muxer.

Uses an isolated socket, configuration, and temporary output directory. Captures
the empty game scene, without connecting to a game or editing Clipper's settings.
"""

import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="clipper-save-smoke-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        config = root / "config/clipper"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({
            "resolution": "320x180", "fps": 15, "video_encoder": "obs_x264",
            "replay_buffer_seconds": 2, "output_format": "mkv",
        }))
        output = root / "clips"
        output.mkdir()
        socket_path = root / "engine.sock"
        with (root / "engine.log").open("w+") as log:
            engine = subprocess.Popen([
                "flatpak", "run", "--user", "--die-with-parent", f"--filesystem={root}",
                "--command=env", "io.github.leesethefox.Clipper",
                f"XDG_CONFIG_HOME={config.parent}", "/app/libexec/clipper/clipper-engine",
                "--socket", str(socket_path), "--output-dir", str(output),
                "--capture-mode", "game_capture",
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
                            print(f"PASS real replay writer: {expected}", flush=True)
                        command("shutdown")
                engine.wait(timeout=15)
                assert engine.returncode == 0, engine.returncode
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
