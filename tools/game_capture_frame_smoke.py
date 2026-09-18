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
import base64
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
import game_capture

CHART_POSITIONS = ((1 / 3, 1 / 8), (2 / 3, 1 / 8), (1 / 3, 7 / 8), (2 / 3, 7 / 8))
CHART_COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))


def receive_until(stream, field, value=None):
    """Skip unrelated events, but surface engine failures and disconnects immediately."""
    while True:
        line = stream.readline()
        if not line:
            raise RuntimeError("engine disconnected")
        message = json.loads(line)
        if message.get("ok") is False or message.get("event") == "clip_save_failed":
            raise RuntimeError(f"engine request failed: {message}")
        if field in message and (value is None or message[field] == value):
            return message


def wait_for_hook(client, stream, game):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        client.sendall(b'{"cmd":"get_status"}\n')
        status = receive_until(stream, "game_hooked")
        if status["game_hooked"]:
            return status
        if game.poll() is not None:
            raise RuntimeError(f"game exited without connecting: {game.returncode}")
        time.sleep(0.1)
    raise RuntimeError("no game connection within 30 seconds")


def verify_preview(client, stream, check_color_chart):
    previews = []
    for _ in range(2):
        client.sendall(b'{"cmd":"get_preview_frame","width":320,"height":180}\n')
        preview = receive_until(stream, "ok")
        if not preview.get("ok") or preview.get("format") != "BGRA":
            raise RuntimeError(f"preview failed: {preview}")
        data = base64.b64decode(preview["data"], validate=True)
        if len(data) != preview["stride"] * preview["height"]:
            raise RuntimeError("preview has invalid dimensions")
        previews.append(data)
        if check_color_chart:
            for (x, y), rgb in zip(CHART_POSITIONS, CHART_COLORS, strict=True):
                offset = (int(y * preview["height"]) * preview["stride"]
                          + int(x * preview["width"]) * 4)
                actual = data[offset:offset + 3][::-1]
                if any(abs(a - b) > 20 for a, b in
                       zip(actual, rgb, strict=True)):
                    raise RuntimeError(
                        "preview color/orientation mismatch"
                    )
        time.sleep(0.1)
    if previews[0] == previews[1]:
        raise RuntimeError("preview is stale")
    print("PASS preview: changing frames and valid dimensions/color")


def verify_cadence(clip, fps, source_fps, copy_pacing):
    pixels = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", clip, "-an",
        "-vf", "crop=16:16:(iw-16)/2:(ih-16)/2,scale=1:1",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ], timeout=30)
    ids = [sum(min(15, pixels[i + channel] // 16) << (channel * 4)
               for channel in range(3))
           for i in range(0, len(pixels), 3)]
    # Exclude startup/preroll while capture attaches.
    steady = ids[min(fps, len(ids) // 3):]
    deltas = [(b - a) % 4096
              for a, b in zip(steady, steady[1:], strict=False)]
    repeats = sum(delta == 0 for delta in deltas)
    backwards = sum(delta > 2048 for delta in deltas)
    if backwards:
        raise RuntimeError(f"frame IDs moved backwards {backwards} times")
    if source_fps >= 2 * fps and repeats > max(1, len(deltas) // 100):
        raise RuntimeError(
            f"too many repeated frame IDs: {repeats}/{len(deltas)}"
        )
    print(f"cadence source_fps={source_fps} recording_fps={fps} "
          f"pacing={copy_pacing} repeats={repeats}/{len(deltas)} "
          f"max_id_step={max(deltas, default=0)}")


def verify_color_chart(clip):
    # Convert before making 1x1 samples: subsampled
    # YUV cannot represent independently stacked 1px tiles.
    filters = "[0:v]format=rgb24,split=4[a][b][c][d];"
    for label, (x, y) in zip("abcd", CHART_POSITIONS, strict=True):
        filters += (f"[{label}]crop=16:16:iw*{x}-8:ih*{y}-8,"
                    f"scale=1:1[{label}p];")
    filters += "[ap][bp][cp][dp]hstack=inputs=4"
    chart = subprocess.check_output([
        "ffmpeg", "-v", "error", "-sseof", "-0.25", "-i", clip,
        "-filter_complex", filters, "-frames:v", "1", "-an",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ], timeout=30)
    expected = bytes(channel for color in CHART_COLORS for channel in color)
    if len(chart) != 12 or any(abs(a - b) > 20 for a, b in
                               zip(chart, expected, strict=True)):
        raise RuntimeError(f"color/orientation mismatch: {list(chart)}")
    print(f"PASS color/orientation: {list(chart)}")


def verify_recording(clip, fps, source_fps, copy_pacing, check_color_chart):
    if check_color_chart:
        metadata = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=color_range,color_space",
            "-of", "json", clip,
        ], text=True, timeout=30))["streams"][0]
        if (metadata.get("color_range") != "pc"
                or metadata.get("color_space") != "bt709"):
            raise RuntimeError(f"incorrect color metadata: {metadata}")
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
    verify_cadence(clip, fps, source_fps, copy_pacing)
    if check_color_chart:
        verify_color_chart(clip)
    return len(frames)


def check(harness: Path, api: str, runtime: Path | None, sandbox: bool, *,
          encoder: str = "obs_x264", resolution: str = "1920x1080", fps: int = 60,
          vaapi_import_cache: bool | None = None, source_fps: int = 60,
          copy_pacing: bool = True, record_seconds: float = 1.5,
          vaapi_direct_surfaces: bool | None = None, check_color_chart: bool = False,
          native_capture: bool | None = None, require_native_capture: bool = False,
          check_preview: bool = False) -> None:
    if check_color_chart and api != "opengl":
        raise ValueError("color chart requires the OpenGL harness")
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
        command = [str(wrapper), str(harness), api, str(source_fps),
                   str(int(record_seconds) + 8)]
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
        (config / "clipper").mkdir()
        (config / "clipper/config.json").write_text(json.dumps({
            "video_encoder": encoder, "resolution": resolution, "fps": fps,
        }))
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
                    "-u", "CLIPPER_VAAPI_IMPORT_CACHE", "-u", "CLIPPER_VAAPI_DIRECT_SURFACES",
                    "-u", "CLIPPER_NATIVE_CAPTURE",
                    f"XDG_CONFIG_HOME={config}",
                    *([f"CLIPPER_VAAPI_IMPORT_CACHE={int(vaapi_import_cache)}"]
                      if vaapi_import_cache is not None else []),
                    *([f"CLIPPER_VAAPI_DIRECT_SURFACES={int(vaapi_direct_surfaces)}"]
                      if vaapi_direct_surfaces is not None else []),
                    f"CLIPPER_HOOK_COPY_PACING={int(copy_pacing)}",
                    *([f"CLIPPER_NATIVE_CAPTURE={int(native_capture)}"]
                      if native_capture is not None else []),
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
            verified_frames = False
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
                    status = wait_for_hook(client, stream, game)
                    if not status["buffer_active"]:
                        client.sendall(b'{"cmd":"start_replay_buffer"}\n')
                        receive_until(stream, "ok")
                    time.sleep(record_seconds)
                    if check_preview:
                        verify_preview(client, stream, check_color_chart)
                    client.sendall(b'{"cmd":"save_replay_buffer"}\n')
                    clip = receive_until(stream, "event", "clip_saved")["path"]
                    frame_count = verify_recording(
                        clip, fps, source_fps, copy_pacing, check_color_chart,
                    )
                    print(
                        f"PASS {api}: decoded {frame_count} changing frames "
                        f"(sandbox={sandbox}, Steam runtime={bool(runtime)})"
                    )
                    verified_frames = True
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
                elog.seek(0)
                engine_log = elog.read()
                for line in engine_log.splitlines():
                    if ("session=" in line or "Clipper VAAPI imports:" in line
                            or "Clipper VAAPI direct" in line or "Clipper native capture:" in line):
                        print(line.strip())
                if verified_frames and require_native_capture:
                    native = re.findall(
                        r"Clipper native capture: bypassed_frames=(\d+)", engine_log
                    )
                    if not native or max(map(int, native)) < fps:
                        raise RuntimeError(
                            "native capture did not bypass at least one second of frames"
                        )
                if native_capture is False and "Clipper native capture: bypassing" in engine_log:
                    raise RuntimeError("native capture ignored its disable switch")
                if verified_frames and "gs_shader_set_val (GL)" in engine_log:
                    raise RuntimeError("shader parameter upload failed")
                glog.seek(0)
                for line in glog:
                    if "Copy pacing:" in line:
                        print(line.strip())
                if (verified_frames and vaapi_direct_surfaces is not False
                        and "vaapi_tex" in encoder):
                    summaries = re.findall(
                        r"Clipper VAAPI direct: acquired=(\d+) submitted=(\d+) "
                        r"copied_frames=(\d+) disabled=(\d+)", engine_log
                    )
                    if not any(int(submitted) > 0 and disabled == "0"
                               for _, submitted, _, disabled in summaries):
                        raise RuntimeError("direct VAAPI path did not submit any frames")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", type=Path)
    parser.add_argument("--api", choices=("opengl", "vulkan"), required=True)
    parser.add_argument("--steam-runtime", type=Path)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--encoder", default="obs_x264")
    parser.add_argument("--resolution", default="1920x1080")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--vaapi-import-cache", action=argparse.BooleanOptionalAction)
    parser.add_argument("--vaapi-direct-surfaces", action=argparse.BooleanOptionalAction)
    parser.add_argument("--native-capture", action=argparse.BooleanOptionalAction)
    parser.add_argument("--require-native-capture", action="store_true")
    parser.add_argument("--check-preview", action="store_true")
    parser.add_argument("--check-color-chart", action="store_true")
    parser.add_argument("--source-fps", type=int, default=60)
    parser.add_argument("--record-seconds", type=float, default=1.5)
    parser.add_argument("--no-copy-pacing", action="store_true")
    args = parser.parse_args()
    check(args.harness.resolve(), args.api, args.steam_runtime, args.sandbox,
          encoder=args.encoder, resolution=args.resolution, fps=args.fps,
          vaapi_import_cache=args.vaapi_import_cache, source_fps=args.source_fps,
          record_seconds=args.record_seconds, copy_pacing=not args.no_copy_pacing,
          vaapi_direct_surfaces=args.vaapi_direct_surfaces,
          check_color_chart=args.check_color_chart, native_capture=args.native_capture,
          require_native_capture=args.require_native_capture, check_preview=args.check_preview)
