"""Integration tests for clipper-engine IPC server.

These tests start the actual engine subprocess and communicate with it over a
Unix domain socket.  They require:
  - The engine binary to be compiled  (engine/src/clipper-engine)
  - The OBS Flatpak to be installed   (/var/lib/flatpak/app/com.obsproject.Studio/…)
  - The lib_shim symlinks to exist    (engine/spike/lib_shim/)

Any missing prerequisite causes the entire module to be skipped with a clear
message rather than failing hard.

Run:
    venv/bin/pytest tests/test_engine_ipc.py -v --timeout=60
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from display_auth import normalize_display_auth_env

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
_ENGINE_BIN = PROJECT_ROOT / "engine" / "src" / "clipper-engine"
_OBS_LIB = Path("/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib")
_OBS_DATA = _OBS_LIB.parent / "share" / "obs"
_VKCAPTURE_FILES = Path(
    "/var/lib/flatpak/runtime/com.obsproject.Studio.Plugin.OBSVkCapture/"
    "x86_64/stable/active/files"
)
_LIB_SHIM = PROJECT_ROOT / "engine" / "spike" / "lib_shim"
_SETUP_SHIM = PROJECT_ROOT / "engine" / "spike" / "setup_lib_shim.sh"
_AGENT_TEST_MODE = os.environ.get("CLIPPER_AGENT_TEST_MODE") == "1"

# ---------------------------------------------------------------------------
# Module-level prerequisite checks — skip the whole module gracefully
# ---------------------------------------------------------------------------

if not _ENGINE_BIN.exists():
    pytest.skip(
        f"Engine binary not found: {_ENGINE_BIN} — build it with `make` in engine/src/",
        allow_module_level=True,
    )

if not _OBS_LIB.exists():
    pytest.skip(
        "OBS Flatpak not installed at expected path — install com.obsproject.Studio via Flatpak",
        allow_module_level=True,
    )

if not _LIB_SHIM.exists() or not any(_LIB_SHIM.iterdir()):
    # Try to build it automatically
    result = subprocess.run(
        ["bash", str(_SETUP_SHIM)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not _LIB_SHIM.exists():
        pytest.skip(
            f"lib_shim setup failed — run `bash engine/spike/setup_lib_shim.sh` "
            f"manually.\n{result.stderr}",
            allow_module_level=True,
        )


# ---------------------------------------------------------------------------
# EngineClient — thin IPC wrapper used by tests
# ---------------------------------------------------------------------------


class EngineClient:
    """Buffered newline-delimited JSON client for the engine's Unix socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(socket_path)
        self._timeout = timeout
        self._buf = b""

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _readline(self, timeout: float) -> str | None:
        """Read one newline-terminated line within *timeout* seconds.

        Uses select() so we can clamp to a deadline without blocking.
        Returns None on timeout or connection close.
        """
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._sock], [], [], remaining)
            if not ready:
                return None
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                return None
            if not chunk:  # EOF — remote closed
                return None
            self._buf += chunk

        idx = self._buf.index(b"\n")
        line = self._buf[:idx].decode("utf-8").strip()
        self._buf = self._buf[idx + 1 :]
        return line

    def send_raw(self, payload: dict) -> None:
        msg = json.dumps(payload) + "\n"
        self._sock.sendall(msg.encode("utf-8"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_event(self, timeout: float = 2.0) -> dict | None:
        """Read one message from the socket (event or response).

        Returns None on timeout or connection close.
        """
        line = self._readline(timeout)
        if line is None:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def send(self, cmd: str, **kwargs: object) -> dict:
        """Send *cmd*, skip any intervening events, return the first response
        that contains an ``ok`` key.

        Raises TimeoutError if no response arrives within self.timeout seconds.
        """
        payload: dict = {"cmd": cmd, **kwargs}
        self.send_raw(payload)
        while True:
            msg = self.read_event(timeout=self._timeout)
            if msg is None:
                raise TimeoutError(f"No response to {cmd!r} within {self._timeout}s")
            if "ok" in msg:
                return msg
            # It's an async event; skip it and keep waiting for the response.

    def read_all_until_response(self, cmd: str, **kwargs: object) -> tuple[list[dict], dict]:
        """Send *cmd*, collect all preceding events, return (events, response).

        Events are messages without an ``ok`` key.  The first message that
        does contain ``ok`` is returned as the response.
        """
        payload: dict = {"cmd": cmd, **kwargs}
        self.send_raw(payload)
        events: list[dict] = []
        while True:
            msg = self.read_event(timeout=self._timeout)
            if msg is None:
                raise TimeoutError(f"No response to {cmd!r} within {self._timeout}s")
            if "ok" in msg:
                return events, msg
            events.append(msg)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Fixture — starts a fresh engine process for each test
# ---------------------------------------------------------------------------


def _build_engine_env() -> dict:
    """Return the environment needed to run the engine."""
    obs_lib = str(_OBS_LIB)
    shim = str(_LIB_SHIM)
    env = os.environ.copy()
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    normalize_display_auth_env(env)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{obs_lib}:{shim}:{shim}/pulseaudio:{existing}"
    env["CLIPPER_OBS_LIBDIR"] = obs_lib
    env["CLIPPER_OBS_DATADIR"] = str(_OBS_DATA)
    env["CLIPPER_VKCAPTURE_PLUGIN"] = str(
        _VKCAPTURE_FILES / "lib/obs-plugins/linux-vkcapture.so"
    )
    env["CLIPPER_VKCAPTURE_PLUGIN_DATA"] = str(
        _VKCAPTURE_FILES / "share/obs/obs-plugins/linux-vkcapture"
    )
    return env


def _running_sink_input_app_names() -> set[str]:
    """Return app names currently reported by pactl sink-input discovery."""
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()

    if result.returncode != 0:
        return set()

    try:
        sink_inputs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()

    names: set[str] = set()
    for sink_input in sink_inputs:
        if not isinstance(sink_input, dict):
            continue
        props = sink_input.get("properties")
        if not isinstance(props, dict):
            continue
        app_name = props.get("application.name")
        if isinstance(app_name, str) and app_name.strip():
            names.add(app_name.strip())
    return names


@pytest.fixture
def engine(tmp_path: Path):
    """Start the engine subprocess, yield (proc, socket_path), kill on teardown."""
    out_dir = tmp_path / "clips"
    out_dir.mkdir()
    socket_path = str(tmp_path / "engine.sock")
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    config_home = tmp_path / "xdg-config"
    config_dir = config_home / "clipper"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "fps": 60,
                "resolution": "2560x1440",
                "replay_buffer_size_mb": 1536,
                "video_encoder": "obs_x264",
                "rate_control": "vbr",
                "video_bitrate": 16000,
                "video_max_bitrate": 24000,
            }
        ),
        encoding="utf-8",
    )
    env = _build_engine_env()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(config_home)

    proc = subprocess.Popen(
        [
            str(_ENGINE_BIN),
            "--socket",
            socket_path,
            "--output-dir",
            str(out_dir),
            "--max-time",
            "5",  # 5-second replay window keeps tests fast
            "--capture-mode",
            "game_capture",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )

    # Use a thread to read the READY line so we can enforce a timeout.
    ready_line: list[str] = []

    def _read_ready() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("READY "):
                ready_line.append(line)
                break

    t = threading.Thread(target=_read_ready, daemon=True)
    t.start()
    t.join(timeout=15.0)

    if not ready_line:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        assert proc.stderr is not None
        stderr = proc.stderr.read()
        if _AGENT_TEST_MODE and "[engine] FAIL: obs_startup" in stderr:
            pytest.skip(
                "engine IPC integration test requires OBS runtime access that is not "
                "available in the agent sandbox"
            )
        pytest.fail(f"Engine did not print READY within 15 s.\nstderr:\n{stderr[:2000]}")

    socket_path_actual = ready_line[0].split()[1]

    yield proc, socket_path_actual

    # Teardown: give the engine a clean death.
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_engine_starts_and_prints_ready(engine: tuple) -> None:
    """Engine process is alive and reported a valid socket path."""
    proc, socket_path = engine
    assert proc.poll() is None, "Engine process died unexpectedly"
    assert socket_path, "Socket path from READY line is empty"
    assert Path(socket_path).exists(), f"Socket file not on disk: {socket_path}"


def test_get_status_idle(engine: tuple) -> None:
    """Fresh engine reports idle status with version info."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("get_status")

    assert resp["ok"] is True
    assert resp["status"] == "idle"
    assert resp["capture_mode"] == "game_capture"
    assert "version" in resp


def test_engine_loads_config_from_xdg_config_home(engine: tuple) -> None:
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("get_status")

    assert resp["rate_control"] == "vbr"
    assert resp["video_bitrate"] == 16000
    assert resp["video_max_bitrate"] == 24000
    assert resp["replay_buffer_size_mb"] == 1536
    assert resp["fps"] == 60
    assert resp["base_width"] == 2560
    assert resp["base_height"] == 1440
    assert resp["output_width"] == 2560
    assert resp["output_height"] == 1440
    assert resp["video_encoder"] == "obs_x264"
    assert isinstance(resp["display_target_selected"], bool)
    assert isinstance(resp["display_target_cancelled"], bool)


def test_repeated_preview_requests_reuse_same_gpu_resources(engine: tuple) -> None:
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        started = client.send("start_replay_buffer")
        assert started["ok"] is True

        successful = []
        for _ in range(20):
            response = client.send("get_preview_frame", width=64, height=36)
            if response["ok"]:
                successful.append(response)
                if len(successful) == 2:
                    break
            time.sleep(0.05)

    assert len(successful) == 2
    first, second = successful
    assert first["ok"] is True
    assert first["resources_reused"] is False
    assert second["ok"] is True
    assert second["resources_reused"] is True
    assert (second["width"], second["height"]) == (64, 36)


def test_get_capabilities(engine: tuple) -> None:
    """Engine reports OBS/runtime capability lists for settings UI."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("get_capabilities")

    assert resp["ok"] is True
    assert isinstance(resp.get("video_encoders"), list)
    assert isinstance(resp.get("audio_encoders"), list)
    assert isinstance(resp.get("formats"), list)
    assert isinstance(resp.get("vaapi_devices"), list)
    assert isinstance(resp.get("capture_modes"), list)
    assert resp["video_encoders"], "Expected at least one video encoder"
    assert resp["audio_encoders"], "Expected at least one audio encoder"
    assert set(resp["formats"]) == {"mkv", "mp4", "mov", "ts"}
    assert "game_capture" in resp["capture_modes"]
    assert any(device.get("id") == "auto" for device in resp["vaapi_devices"])

    video_encoder = resp["video_encoders"][0]
    assert {"id", "name", "codec", "hardware"} <= set(video_encoder)

    audio_encoder = resp["audio_encoders"][0]
    assert {"id", "name", "codec", "hardware"} <= set(audio_encoder)


def test_get_audio_capabilities(engine: tuple) -> None:
    """Engine reports audio routing limits and registered OBS audio source IDs."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("get_audio_capabilities")

    assert resp["ok"] is True
    assert resp["max_track_count"] == 6
    assert isinstance(resp.get("source_ids"), list)
    assert isinstance(resp.get("supported_backends"), list)
    assert isinstance(resp.get("app_capture_available"), bool)


def test_list_audio_sources(engine: tuple) -> None:
    """Engine returns a stable list_audio_sources response shape."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("list_audio_sources")

    assert resp["ok"] is True
    assert isinstance(resp.get("sources"), list)
    for source in resp["sources"]:
        assert {"id", "kind", "display_name", "backend"} <= set(source)


def test_list_audio_sources_includes_running_sink_input_apps(engine: tuple) -> None:
    """Engine discovery includes applications currently playing audio."""
    expected_names = _running_sink_input_app_names()
    if not expected_names:
        pytest.skip("No active pactl sink-input applications to assert against")

    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("list_audio_sources")

    discovered_names = {
        source.get("app_name") or source.get("display_name")
        for source in resp.get("sources", [])
        if source.get("kind") == "application"
    }

    assert expected_names <= discovered_names


def test_start_replay_buffer(engine: tuple) -> None:
    """start_replay_buffer returns ok=true; get_status then reports active."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("start_replay_buffer")
        assert resp["ok"] is True

        # Status should now be active (send() skips any status_changed events).
        status = client.send("get_status")

    assert status["ok"] is True
    assert status["status"] == "active"


def test_start_already_running(engine: tuple) -> None:
    """Starting an already-running buffer returns ok=false with an error."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp1 = client.send("start_replay_buffer")
        assert resp1["ok"] is True

        resp2 = client.send("start_replay_buffer")

    assert resp2["ok"] is False
    assert "already running" in resp2.get("error", "")


def test_stop_replay_buffer(engine: tuple) -> None:
    """stop_replay_buffer returns ok=true and status returns to idle."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        client.send("start_replay_buffer")

        resp = client.send("stop_replay_buffer")
        assert resp["ok"] is True

        # send() skips any status_changed events; get_status should be idle.
        status = client.send("get_status")

    assert status["ok"] is True
    assert status["status"] == "idle"


def test_stop_not_running(engine: tuple) -> None:
    """Stopping when no buffer is running returns ok=false."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("stop_replay_buffer")

    assert resp["ok"] is False
    assert "not running" in resp.get("error", "")


def test_save_replay_buffer(engine: tuple) -> None:
    """start → wait → save → clip_saved event → file on disk."""
    _, socket_path = engine
    # The fixture places clips in <tmp_path>/clips/, where <tmp_path> is the
    # parent of the socket file.
    clips_dir = Path(socket_path).parent / "clips"

    with EngineClient(socket_path) as client:
        resp = client.send("start_replay_buffer")
        assert resp["ok"] is True

        # Buffer needs at least one keyframe (~2 s at 30 fps default GOP).
        time.sleep(2)

        resp = client.send("save_replay_buffer")
        assert resp["ok"] is True, f"save_replay_buffer failed: {resp}"

        # The clip_saved event arrives asynchronously after OBS writes the file.
        clip_path: str | None = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            evt = client.read_event(timeout=remaining)
            if evt is None:
                break
            if evt.get("event") == "clip_saved":
                clip_path = evt.get("path", "")
                break

    assert clip_path is not None, "Did not receive clip_saved event within 15 s"
    assert clip_path, "clip_saved event had an empty path"
    clip_file = Path(clip_path)
    assert clip_file.exists(), f"Clip file not found on disk: {clip_path}"
    assert clip_file.parent.resolve() == clips_dir.resolve(), (
        f"Clip saved to unexpected directory: {clip_file.parent} (expected {clips_dir})"
    )


def test_save_not_running(engine: tuple) -> None:
    """Saving when no buffer is running returns ok=false."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("save_replay_buffer")

    assert resp["ok"] is False
    assert "not running" in resp.get("error", "")


def test_shutdown_command(engine: tuple) -> None:
    """shutdown returns ok=true and the process exits cleanly within 5 s."""
    proc, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("shutdown")

    assert resp["ok"] is True

    ret = None
    try:
        ret = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail("Engine did not exit within 5 s after shutdown command")

    assert ret == 0, f"Engine exited with non-zero code {ret}"


def test_multiple_clients(engine: tuple) -> None:
    """Two simultaneous clients each receive a valid response."""
    _, socket_path = engine
    results: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def run_a() -> None:
        try:
            with EngineClient(socket_path) as c:
                # send() silently skips any status_changed events from client B.
                results["a"] = c.send("get_status")
        except Exception as exc:  # noqa: BLE001
            errors["a"] = exc

    def run_b() -> None:
        try:
            with EngineClient(socket_path) as c:
                results["b"] = c.send("start_replay_buffer")
        except Exception as exc:  # noqa: BLE001
            errors["b"] = exc

    ta = threading.Thread(target=run_a)
    tb = threading.Thread(target=run_b)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    assert not errors, f"Client errors: {errors}"
    assert results.get("a", {}).get("ok") is True, f"Client A response: {results.get('a')}"
    assert results.get("b", {}).get("ok") is True, f"Client B response: {results.get('b')}"


def test_unknown_command(engine: tuple) -> None:
    """An unrecognised command returns ok=false with an error field."""
    _, socket_path = engine
    with EngineClient(socket_path) as client:
        resp = client.send("bogus")

    assert resp["ok"] is False
    assert "error" in resp
