# Clipper UI

The UI is a Python application built with GTK4 and libadwaita. It owns the things people interact with: configuration, game detection, hotkeys, tray integration, notifications, and the lifetime of the recording engine.

The engine runs separately. UI and engine exchange newline-delimited JSON over a Unix socket, so the GTK process does not need to carry the OBS/libobs work.

## Layout

- `main.py` runs the application, coordinates capture rules, registers hotkeys, shows notifications, and decides when to start or stop the engine.
- `main_window.py` owns the **Clips**, **Games**, and **Audio** pages, the **Preferences** dialog, and the engine status indicator.
- `engine_manager.py` starts `clipper-engine` and reports readiness and process exit. `engine_client.py` fetches state on connection and receives subsequent status events; the UI does not periodically poll the engine.
- `monitor_manager.py` runs the same process-monitor helper on the host in Flatpak builds and directly in native runs. Complete snapshots restore game state after reconnects.
- `config.py` writes JSON configuration atomically under the XDG config directory.

## Clip editor

The `editor_*.py` modules hold project, history, probing, draft, waveform, timeline, and export code that does not depend on GTK. Clipper loads the editor window only after someone chooses **Edit clip**.

Editor times are integer microseconds. Drafts live outside the main configuration, and exports write a unique partial file before atomically replacing it with the finished file.

## Capture modes

Each game rule uses one of these modes:

- **Display capture** is the default. It records a selected display through the desktop portal and PipeWire. A host process monitor decides when Clipper should start and stop recording.
- **Game capture** uses Clipper's bundled Vulkan/OpenGL hooks. Native and Flatpak Steam launch options are configured automatically, with one restart confirmation if needed. The engine waits for game frames before recording.

Display capture works with more games, but records the selected display rather than a single window. Game capture has launch-time and compatibility limits. Do not use capture hooks with a game whose anti-cheat policy forbids them.

## Audio

The Audio page supports one mixed track or up to six separate tracks. It discovers available sources when opened and refreshes them on audio events, querying a connected engine or using `pactl` directly without starting one. It stores the chosen routing in the configuration, applies volume changes live, and asks for an engine restart when a source or route changes.

## Run it locally

From the repository root:

```bash
make -C engine/src
./clipper
./tools/run_pytest_quiet.sh
```

The native launcher uses `./venv` when it can import PyGObject. It also expects a locally installed OBS Studio Flatpak and OBSVkCapture extension. The standalone Clipper Flatpak bundles that recording stack.

## Manual validation

When changing capture, game detection, audio routing, or setup, check the
affected flow in the installed Flatpak with real desktop and game processes:

- Add a display-capture rule, pick a display, launch the game, and save a clip.
- Check game detection with the affected native Steam, Flatpak Steam, Proton,
  launcher-child, or same-name process setup.
- Check separate tracks with real application and Proton/Wine audio streams.
- Check first-run setup in a clean Flatpak account without OBS Studio installed.

For Flatpak commands and review rationale, see `packaging/flatpak/`.
