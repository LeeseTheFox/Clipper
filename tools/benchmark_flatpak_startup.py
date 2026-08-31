#!/usr/bin/env python3
"""Portable presentation benchmark for the installed Clipper Flatpak.

The benchmark launches Clipper through its exported desktop entry and opens or
closes it through the standard StatusNotifierItem interface. Clipper publishes
GTK frame-submission timestamps through a read-only application action, so no
desktop-specific input emulation, screenshot, or screen-capture code is needed.

Frame submission is observable inside GTK but physical window visibility is a
shell/compositor decision. This portable mode deliberately does not label the
first-frame value as visible-window latency.

"Cold" means no Clipper process is running. It does not mean that the kernel's
filesystem cache has been flushed. "Tray" means Clipper reached the background
through its normal close-to-tray handoff from a populated window.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GioUnix", "2.0")

from gi.repository import Gio, GioUnix, GLib

APP_ID = "io.github.leesethefox.Clipper"
APP_OBJECT = "/io/github/leesethefox/Clipper"
DESKTOP_ID = f"{APP_ID}.desktop"
PRESENTATION_ACTION = "presentation-state"
SNI_INTERFACE = "org.kde.StatusNotifierItem"
SNI_WATCHER = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"
VIDEO_SUFFIXES = {".mkv", ".mp4", ".mov", ".flv", ".ts"}


class BenchmarkError(RuntimeError):
    """A concise benchmark failure suitable for terminal output."""


@dataclass(frozen=True)
class Result:
    window_ms: float
    clips_ms: float

    @property
    def clips_after_window_ms(self) -> float:
        return max(0.0, self.clips_ms - self.window_ms)


@dataclass(frozen=True)
class StatusItem:
    service: str
    path: str


def run_quiet(command: list[str], *, timeout: float = 3.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


def app_is_running() -> bool:
    result = run_quiet(["flatpak", "ps", "--columns=application"])
    return APP_ID in result.stdout.splitlines()


def stop_clipper(timeout: float) -> None:
    if app_is_running():
        run_quiet(["flatpak", "kill", APP_ID])
    deadline = time.monotonic() + timeout
    while app_is_running():
        if time.monotonic() >= deadline:
            raise BenchmarkError("Clipper did not stop")
        time.sleep(0.03)


def clip_count() -> int:
    config_path = (
        Path.home()
        / ".var"
        / "app"
        / APP_ID
        / "config"
        / "clipper"
        / "config.json"
    )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        output_folder = Path(config["output_folder"])
        return sum(
            1
            for path in output_folder.iterdir()
            if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return 0


def process_rows() -> list[tuple[str, str]]:
    result = run_quiet(["ps", "-eo", "comm=,args="], timeout=2.0)
    if result.returncode != 0:
        raise BenchmarkError("could not inspect Clipper's process state")
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if fields:
            rows.append((fields[0], fields[1] if len(fields) > 1 else ""))
    return rows


def engine_is_running() -> bool:
    return any(command == "clipper-engine" for command, _args in process_rows())


def background_handoff_is_running() -> bool:
    return any(
        command.startswith("python")
        and re.search(r"(?:^|/)main\.py\s+--background(?:\s|$)", arguments)
        for command, arguments in process_rows()
    )


def wait_for_idle_engine(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        if engine_is_running():
            idle_since = None
        else:
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= 0.3:
                return
        time.sleep(0.05)
    raise BenchmarkError(
        "Clipper's capture engine stayed active; stop any whitelisted game "
        "before benchmarking tray startup"
    )


class DesktopSession:
    """Use only freedesktop/GIO interfaces shared by Linux desktops."""

    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def _call(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant,
        *,
        timeout_ms: int = 1_000,
    ) -> GLib.Variant:
        return self.bus.call_sync(
            service,
            path,
            interface,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )

    def name_has_owner(self, name: str) -> bool:
        reply = self._call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (name,)),
        )
        return bool(reply.unpack()[0])

    def registered_status_items(self) -> list[StatusItem]:
        try:
            reply = self._call(
                SNI_WATCHER,
                SNI_WATCHER_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant(
                    "(ss)",
                    (SNI_WATCHER, "RegisteredStatusNotifierItems"),
                ),
            )
        except GLib.Error:
            return []

        items = []
        for value in reply.unpack()[0]:
            service, separator, path_tail = value.partition("/")
            if service and separator and path_tail:
                items.append(StatusItem(service, f"/{path_tail}"))
        return items

    def status_item_title(self, item: StatusItem) -> str:
        reply = self._call(
            item.service,
            item.path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (SNI_INTERFACE, "Title")),
            timeout_ms=250,
        )
        return str(reply.unpack()[0])

    def find_clipper_status_item(
        self,
        timeout: float,
        *,
        exclude_service: str | None = None,
    ) -> StatusItem:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in self.registered_status_items():
                if item.service == exclude_service:
                    continue
                try:
                    if self.status_item_title(item) == "Clipper":
                        return item
                except GLib.Error:
                    continue
            time.sleep(0.03)
        raise BenchmarkError(
            "Clipper did not register a StatusNotifier tray item; this desktop "
            "needs a compatible tray host to benchmark tray opening"
        )

    def presentation_state(self, service: str) -> dict[str, int] | None:
        try:
            reply = self._call(
                service,
                APP_OBJECT,
                "org.gtk.Actions",
                "Describe",
                GLib.Variant("(s)", (PRESENTATION_ACTION,)),
                timeout_ms=250,
            )
            description = reply.unpack()[0]
            values = description[2]
            if not values:
                return None
            state = json.loads(values[0])
            if not isinstance(state, dict):
                return None
            return {
                "pid": int(state.get("pid", 0)),
                "window": int(state.get("window", 0)),
                "clips": int(state.get("clips", 0)),
            }
        except (GLib.Error, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def launch_desktop_entry(self) -> int:
        app_info = GioUnix.DesktopAppInfo.new(DESKTOP_ID)
        if app_info is None:
            raise BenchmarkError(f"could not find the installed {DESKTOP_ID} entry")

        # The launched app would otherwise inherit this tool's console. Desktop
        # launchers do not give GUI applications a benchmark terminal either.
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        try:
            with open(os.devnull, "w", encoding="utf-8") as devnull:
                os.dup2(devnull.fileno(), 1)
                os.dup2(devnull.fileno(), 2)
                start_ns = time.monotonic_ns()
                launched = app_info.launch([], Gio.AppLaunchContext())
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        if not launched:
            raise BenchmarkError("the installed Clipper desktop entry did not launch")
        return start_ns

    def activate_status_item(self, item: StatusItem) -> int:
        start_ns = time.monotonic_ns()
        self._call(
            item.service,
            item.path,
            SNI_INTERFACE,
            "Activate",
            GLib.Variant("(ii)", (0, 0)),
        )
        return start_ns


def wait_for_app_bus(session: DesktopSession, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_is_running() and session.name_has_owner(APP_ID):
            return
        time.sleep(0.02)
    raise BenchmarkError("Clipper did not acquire its application D-Bus name")


def wait_for_result(
    session: DesktopSession,
    service: str,
    start_ns: int,
    timeout: float,
) -> Result:
    deadline = time.monotonic() + timeout
    saw_action = False
    while time.monotonic() < deadline:
        state = session.presentation_state(service)
        if state is not None:
            saw_action = True
            window_ns = state["window"]
            clips_ns = state["clips"]
            if window_ns and clips_ns:
                if window_ns < start_ns or clips_ns < start_ns:
                    raise BenchmarkError(
                        "Clipper reported stale presentation data from an earlier window"
                    )
                return Result(
                    (window_ns - start_ns) / 1_000_000,
                    (clips_ns - start_ns) / 1_000_000,
                )
        time.sleep(0.004)

    if not saw_action:
        raise BenchmarkError(
            "the installed Clipper does not expose presentation timestamps; "
            "build and install this revision before benchmarking"
        )
    raise BenchmarkError("timed out waiting for Clipper's presented window and clips")


def enter_background_handoff(
    session: DesktopSession,
    visible_item: StatusItem,
    timeout: float,
) -> StatusItem:
    wait_for_idle_engine(timeout)
    session.activate_status_item(visible_item)
    deadline = time.monotonic() + timeout
    new_item = None
    stable_since = None
    while time.monotonic() < deadline:
        if new_item is None:
            try:
                new_item = session.find_clipper_status_item(
                    min(0.5, max(0.05, deadline - time.monotonic())),
                    exclude_service=visible_item.service,
                )
            except BenchmarkError:
                pass
        state = (
            session.presentation_state(new_item.service)
            if new_item is not None
            else None
        )
        ready = (
            new_item is not None
            and background_handoff_is_running()
            and state is not None
            and state["window"] == 0
            and state["clips"] == 0
        )
        if ready:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 0.25:
                assert new_item is not None
                return new_item
        else:
            stable_since = None
        time.sleep(0.03)
    raise BenchmarkError(
        "Clipper did not enter its verified close-to-tray handoff state; "
        "tray timing would not be comparable"
    )


def print_result(kind: str, index: int, trials: int, result: Result) -> None:
    print(
        f"{kind:5} {index}/{trials}: "
        f"first frame {result.window_ms / 1000:.3f}s, "
        f"clips {result.clips_ms / 1000:.3f}s "
        f"(+{result.clips_after_window_ms / 1000:.3f}s)"
    )


def print_summary(kind: str, results: list[Result]) -> None:
    windows = [result.window_ms for result in results]
    clips = [result.clips_ms for result in results]
    delays = [result.clips_after_window_ms for result in results]

    def compact(values: list[float]) -> str:
        return (
            f"median {statistics.median(values) / 1000:.3f}s, "
            f"range {min(values) / 1000:.3f}-{max(values) / 1000:.3f}s"
        )

    print(
        f"{kind:5} summary: first frame {compact(windows)}; "
        f"clips {compact(clips)}; after first frame {compact(delays)}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Clipper through portable desktop interfaces."
    )
    parser.add_argument(
        "--trials", type=int, default=5, help="trials per mode (default: 5)"
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="seconds allowed per trial"
    )
    parser.add_argument(
        "--settle", type=float, default=0.5, help="seconds between desktop actions"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.trials < 1 or args.timeout <= 0 or args.settle < 0:
        print(
            "error: trials and timeout must be positive; settle cannot be negative",
            file=sys.stderr,
        )
        return 2

    required = ("flatpak", "ps")
    missing = [command for command in required if not shutil.which(command)]
    if missing:
        print(f"error: missing required command(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    if run_quiet(["flatpak", "info", APP_ID]).returncode != 0:
        print(f"error: {APP_ID} is not installed", file=sys.stderr)
        return 2
    clips = clip_count()
    if clips == 0:
        print("error: no clips found in Clipper's configured output folder", file=sys.stderr)
        return 2

    session = DesktopSession()
    cold_results: list[Result] = []
    tray_results: list[Result] = []
    visible_item = None
    try:
        print(
            f"Clipper portable benchmark: {args.trials} trials/mode, {clips} clips, "
            "desktop entry + StatusNotifier, no screen capture"
        )
        print("Note: first frame is GTK submission, not compositor-visible window time.")
        print(
            "States: cold = no Clipper process; tray = normal populated-window "
            "close handoff"
        )

        for index in range(1, args.trials + 1):
            stop_clipper(args.timeout)
            time.sleep(args.settle)
            start_ns = session.launch_desktop_entry()
            wait_for_app_bus(session, args.timeout)
            result = wait_for_result(session, APP_ID, start_ns, args.timeout)
            cold_results.append(result)
            print_result("cold", index, args.trials, result)

        visible_item = session.find_clipper_status_item(args.timeout)
        background_item = enter_background_handoff(
            session, visible_item, args.timeout
        )
        time.sleep(args.settle)

        for index in range(1, args.trials + 1):
            start_ns = session.activate_status_item(background_item)
            result = wait_for_result(
                session, background_item.service, start_ns, args.timeout
            )
            tray_results.append(result)
            print_result("tray", index, args.trials, result)
            background_item = enter_background_handoff(
                session, background_item, args.timeout
            )
            time.sleep(args.settle)

        print_summary("cold", cold_results)
        print_summary("tray", tray_results)
        return 0
    except (BenchmarkError, GLib.Error, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(BenchmarkError):
            stop_clipper(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
