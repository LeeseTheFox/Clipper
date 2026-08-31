import io
from pathlib import Path
from threading import Event

from engine_manager import FLATPAK_ENGINE_PATH, EngineProcessManager


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def kill(self):
        self.killed = True
        self.exit_code = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.exit_code


def _repo_with_engine(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    engine = repo / "engine" / "src" / "clipper-engine"
    engine.parent.mkdir(parents=True)
    engine.write_text("#!/bin/sh\n", encoding="utf-8")
    lib_shim = repo / "engine" / "spike" / "lib_shim"
    (lib_shim / "pulseaudio").mkdir(parents=True)
    return repo, engine


def test_start_uses_engine_binary_and_runtime_env(tmp_path, monkeypatch):
    repo, engine = _repo_with_engine(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    xauth = runtime / "xauth_valid"
    xauth.write_text("", encoding="utf-8")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing")
    monkeypatch.setenv("XAUTHORITY", str(tmp_path / "missing-xauth"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    calls = []

    def popen(cmd, cwd, env):
        calls.append((cmd, cwd, env))
        return FakeProcess()

    manager = EngineProcessManager(engine_path=engine, repo_root=repo, popen_factory=popen)

    assert manager.start() is True

    cmd, cwd, env = calls[0]
    assert cmd == [str(engine), "--capture-mode", "display_capture"]
    assert cwd == str(repo)
    assert str(repo / "engine" / "spike" / "lib_shim") in env["LD_LIBRARY_PATH"]
    assert str(repo / "engine" / "spike" / "lib_shim" / "pulseaudio") in env[
        "LD_LIBRARY_PATH"
    ]
    assert "/existing" in env["LD_LIBRARY_PATH"]
    assert env["XAUTHORITY"] == str(xauth)


def test_flatpak_mode_defaults_to_app_engine_path(tmp_path):
    manager = EngineProcessManager(
        repo_root=tmp_path,
        env={"FLATPAK_ID": "io.github.leesethefox.Clipper"},
    )

    assert manager.engine_path == FLATPAK_ENGINE_PATH


def test_flatpak_mode_does_not_add_development_library_paths(tmp_path):
    engine = tmp_path / "clipper-engine"
    engine.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    def popen(cmd, cwd, env):
        calls.append((cmd, cwd, env))
        return FakeProcess()

    manager = EngineProcessManager(
        engine_path=engine,
        repo_root=tmp_path,
        popen_factory=popen,
        env={
            "FLATPAK_ID": "io.github.leesethefox.Clipper",
            "LD_LIBRARY_PATH": "/keep",
        },
    )

    assert manager.start() is True

    env = calls[0][2]
    assert env["LD_LIBRARY_PATH"] == "/keep"
    assert not any(
        "com.obsproject.Studio" in str(value) for value in env.values()
    )


def test_start_forwards_engine_output_to_log_callback(tmp_path):
    repo, engine = _repo_with_engine(tmp_path)
    log_received = Event()
    logs = []

    def popen(cmd, **kwargs):
        assert cmd == [str(engine), "--capture-mode", "display_capture"]
        assert kwargs["stdout"] is not None
        assert kwargs["stderr"] is not None
        assert kwargs["text"] is True
        process = FakeProcess()
        process.stdout = io.StringIO("engine ready\nengine recording\n")
        return process

    def log_callback(message):
        logs.append(message)
        if len(logs) == 2:
            log_received.set()

    manager = EngineProcessManager(
        engine_path=engine,
        repo_root=repo,
        popen_factory=popen,
        log_callback=log_callback,
    )

    assert manager.start() is True
    assert log_received.wait(1)
    assert logs == ["engine: engine ready", "engine: engine recording"]


def test_poll_restarts_engine_when_requested(tmp_path):
    repo, engine = _repo_with_engine(tmp_path)
    processes = [FakeProcess(), FakeProcess()]
    calls = []

    def popen(cmd, cwd, env):
        calls.append(cmd)
        return processes[len(calls) - 1]

    manager = EngineProcessManager(engine_path=engine, repo_root=repo, popen_factory=popen)
    manager.start("game_capture")
    processes[0].exit_code = 42

    assert manager.poll() == 42
    assert len(calls) == 2
    assert manager.is_running()
    assert calls[0] == [str(engine), "--capture-mode", "game_capture"]
    assert calls[1] == [str(engine), "--capture-mode", "game_capture"]


def test_poll_does_not_restart_after_normal_exit(tmp_path):
    repo, engine = _repo_with_engine(tmp_path)
    process = FakeProcess()
    calls = []

    def popen(cmd, cwd, env):
        calls.append(cmd)
        return process

    manager = EngineProcessManager(engine_path=engine, repo_root=repo, popen_factory=popen)
    manager.start()
    process.exit_code = 0

    assert manager.poll() == 0
    assert len(calls) == 1
    assert not manager.is_running()


def test_start_switches_capture_mode_by_restarting_engine(tmp_path):
    repo, engine = _repo_with_engine(tmp_path)
    processes = [FakeProcess(), FakeProcess()]
    calls = []

    def popen(cmd, cwd, env):
        calls.append(cmd)
        return processes[len(calls) - 1]

    manager = EngineProcessManager(engine_path=engine, repo_root=repo, popen_factory=popen)
    manager.start("display_capture")
    manager.start("game_capture")

    assert processes[0].terminated
    assert processes[0].waited
    assert manager.is_running()
    assert calls == [
        [str(engine), "--capture-mode", "display_capture"],
        [str(engine), "--capture-mode", "game_capture"],
    ]


def test_terminate_stops_running_engine(tmp_path):
    repo, engine = _repo_with_engine(tmp_path)
    process = FakeProcess()

    manager = EngineProcessManager(
        engine_path=engine,
        repo_root=repo,
        popen_factory=lambda cmd, cwd, env: process,
    )
    manager.start()

    manager.terminate()

    assert process.terminated
    assert process.waited
    assert not manager.is_running()
