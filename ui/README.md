# Clipper UI

The UI is a Python application built with GTK4 and libadwaita. It owns the things people interact with: configuration, game detection, hotkeys, tray integration, notifications, and the lifetime of the recording engine.

The engine runs separately. UI and engine exchange newline-delimited JSON over a Unix socket, so the GTK process does not need to carry the OBS/libobs work.

## Layout

- `main.py` runs the application, coordinates capture rules, registers hotkeys, shows notifications, and decides when to start or stop the engine.
- `main_window.py` owns the **Clips**, **Games**, and **Audio** pages, the **Preferences** dialog, and the engine status indicator.
- `engine_manager.py` starts `clipper-engine` for the chosen capture mode. `engine_client.py` is the IPC client.
- `monitor_manager.py` runs the fixed host-monitor helper in Flatpak builds. Native runs use the local monitoring path.
- `config.py` writes JSON configuration atomically under the XDG config directory.

## Clip editor

The `editor_*.py` modules hold project, history, probing, draft, waveform, timeline, and export code that does not depend on GTK. Clipper loads the editor window only after someone chooses **Edit clip**.

Editor times are integer microseconds. Drafts live outside the main configuration, and exports write a unique partial file before atomically replacing it with the finished file.

## Capture modes

Each game rule uses one of these modes:

- **Display capture** is the default. It records a selected display through the desktop portal and PipeWire. A host process monitor decides when Clipper should start and stop recording.
- **Game capture (advanced)** uses Clipper's bundled Vulkan/OpenGL hooks. Native and Flatpak Steam launch options are configured automatically, with one restart confirmation if needed. The engine waits for game frames before recording.

Display capture works with more games, but records the selected display rather than a single window. Game capture has launch-time and compatibility limits. Do not use capture hooks with a game whose anti-cheat policy forbids them.

## Audio

The Audio page supports one mixed track or up to six separate tracks. It asks the engine which sources are available, stores the chosen routing in the configuration, applies volume changes live, and asks for an engine restart when a source or route changes.

## Run it locally

From the repository root:

```bash
make -C engine/src
./clipper
./tools/run_pytest_quiet.sh
```

The native launcher uses `./venv` when it can import PyGObject. It also expects a locally installed OBS Studio Flatpak and OBSVkCapture extension. The standalone Clipper Flatpak bundles that recording stack.

## Still worth testing

- A complete display-capture flow: add a rule, pick a display, launch the game, and save a clip.
- Game detection for native Steam, Flatpak Steam, Proton, launcher-child processes, and process-name collisions.
- Separate audio tracks with real application and Proton/Wine audio streams.
- A clean-account Flatpak install with no OBS Studio installation.
- AppStream screenshots and a public canonical app URL before Flathub submission.

For Flatpak commands and review rationale, see `packaging/flatpak/`.
