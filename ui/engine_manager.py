"""On-demand lifecycle management for the OBS-backed Clipper engine."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from capture_modes import DEFAULT_CAPTURE_MODE, normalize_capture_mode
from display_auth import normalize_display_auth_env

RESTART_EXIT_CODE = 42
FLATPAK_ENGINE_PATH = Path("/app/libexec/clipper/clipper-engine")


def running_in_flatpak(env: dict[str, str] | None = None) -> bool:
    """Return True when Clipper is running inside its Flatpak sandbox."""
    environment = env if env is not None else os.environ
    return bool(environment.get("FLATPAK_ID"))


class EngineProcessManager:
    """Start, monitor, and stop ``clipper-engine`` when capture is needed."""

    def __init__(
        self,
        engine_path: Path | None = None,
        repo_root: Path | None = None,
        popen_factory: Any = subprocess.Popen,
        env: dict[str, str] | None = None,
        log_callback: Any | None = None,
    ) -> None:
        self._base_env = env
        self._repo_root = repo_root or Path(__file__).resolve().parent.parent
        if engine_path is not None:
            self._engine_path = engine_path
        elif running_in_flatpak(self._base_env):
            self._engine_path = FLATPAK_ENGINE_PATH
        else:
            self._engine_path = self._repo_root / "engine" / "src" / "clipper-engine"
        self._popen_factory = popen_factory
        self._process: Any | None = None
        self._capture_mode = DEFAULT_CAPTURE_MODE
        self._log_callback = log_callback

    @property
    def engine_path(self) -> Path:
        return self._engine_path

    @property
    def capture_mode(self) -> str:
        return self._capture_mode

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, capture_mode: str = DEFAULT_CAPTURE_MODE) -> bool:
        """Start the engine if it is not already running."""
        capture_mode = normalize_capture_mode(capture_mode)
        self.poll()
        if self.is_running() and self._capture_mode == capture_mode:
            return True
        if self.is_running():
            self.terminate()

        if not self._engine_path.exists():
            raise FileNotFoundError(f"Engine binary not found: {self._engine_path}")

        if not running_in_flatpak(self._base_env):
            self._ensure_lib_shim()
        self._capture_mode = capture_mode
        popen_kwargs = {
            "cwd": str(self._repo_root),
            "env": self._build_env(),
        }
        if self._log_callback is not None:
            popen_kwargs.update(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        self._process = self._popen_factory(
            [str(self._engine_path), "--capture-mode", capture_mode], **popen_kwargs
        )
        if self._log_callback is not None:
            self._start_log_reader(self._process)
        return True

    def _start_log_reader(self, process: Any) -> None:
        """Forward the engine's combined stdout/stderr without blocking the UI."""
        output = getattr(process, "stdout", None)
        log_callback = self._log_callback
        if output is None or log_callback is None:
            return

        def read_output() -> None:
            for line in output:
                log_callback(f"engine: {line.rstrip()}")

        threading.Thread(target=read_output, daemon=True).start()

    def poll(self) -> int | None:
        """Observe process exit; restart when the engine requested it."""
        if self._process is None:
            return None

        exit_code = self._process.poll()
        if exit_code is None:
            return None

        self._process = None
        if exit_code == RESTART_EXIT_CODE:
            self.start(self._capture_mode)
        return exit_code

    def terminate(self, timeout: float = 5.0) -> None:
        """Terminate the managed engine process if it is still alive."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _build_env(self) -> dict[str, str]:
        env = dict(self._base_env) if self._base_env is not None else os.environ.copy()
        normalize_display_auth_env(env)
        if running_in_flatpak(env):
            return env

        paths: list[str] = []

        obs_lib = Path(
            "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
        )
        if obs_lib.exists():
            paths.append(str(obs_lib))
            env.setdefault("CLIPPER_OBS_LIBDIR", str(obs_lib))
            env.setdefault(
                "CLIPPER_OBS_DATADIR",
                "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/share/obs",
            )

        vkc_ext = Path(
            "/var/lib/flatpak/runtime/com.obsproject.Studio.Plugin.OBSVkCapture/x86_64/stable/active/files"
        )
        if vkc_ext.exists():
            env.setdefault(
                "CLIPPER_VKCAPTURE_PLUGIN",
                str(vkc_ext / "lib" / "obs-plugins" / "linux-vkcapture.so"),
            )
            env.setdefault(
                "CLIPPER_VKCAPTURE_PLUGIN_DATA",
                str(vkc_ext / "share" / "obs" / "obs-plugins" / "linux-vkcapture"),
            )

        lib_shim = self._repo_root / "engine" / "spike" / "lib_shim"
        if lib_shim.exists():
            paths.extend([str(lib_shim), str(lib_shim / "pulseaudio")])

        existing = env.get("LD_LIBRARY_PATH")
        if existing:
            paths.append(existing)

        if paths:
            env["LD_LIBRARY_PATH"] = ":".join(paths)

        return env

    def _ensure_lib_shim(self) -> None:
        lib_shim = self._repo_root / "engine" / "spike" / "lib_shim"
        setup_script = self._repo_root / "engine" / "spike" / "setup_lib_shim.sh"
        if lib_shim.exists() or not setup_script.exists():
            return

        subprocess.run(["bash", str(setup_script)], cwd=str(self._repo_root), check=False)
