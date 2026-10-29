# Clipper UI

The Python UI uses GTK4 and libadwaita. It manages settings, game detection,
hotkeys, the tray, notifications, and the recording engine process.

The engine runs in a separate process. It communicates with the UI using
newline-delimited JSON over a Unix socket.

## Modules

- `main.py` coordinates startup, capture rules, hotkeys, notifications, and
  engine startup and shutdown.
- `main_window.py` builds the **Clips**, **Games**, and **Audio** pages,
  **Preferences**, and the engine status indicator.
- `engine_manager.py` starts `clipper-engine` and reports readiness and exit.
  `engine_client.py` fetches state on connection and receives later updates
  as events, without polling.
- `monitor_manager.py` runs the process monitor on the host for Flatpak builds
  and directly for native runs. After reconnecting, it restores game state
  from a complete snapshot.
- `config.py` writes JSON settings atomically under the XDG config directory.

## Clip editor

The `editor_*.py` modules contain project, history, media probing, draft,
waveform, timeline, and export code that does not depend on GTK. The editor
window loads when the user chooses **Edit clip**.

Editor times use integer microseconds. Drafts are stored separately from the
main configuration. Exports write to a unique partial file, then atomically
rename the completed file to its destination.

## Capture modes

Each game has a capture mode:

- **Display capture**, the default, records the selected display. On Wayland,
  it uses the desktop portal and PipeWire. The process monitor controls when
  recording starts and stops.
- **Game capture** records the game using the bundled Vulkan/OpenGL hooks.
  Clipper configures native and Flatpak Steam launch options and asks for a
  Steam restart if needed. Recording starts when the engine receives game frames.

Display capture works with more games and includes everything visible on the
selected screen. Game capture requires launch options and compatible graphics
and drivers. Follow the game's anti-cheat policy when using capture hooks.

## Audio

The Audio page supports one mixed track or up to six separate tracks. It
discovers sources when opened and refreshes them in response to audio events.
Discovery queries the engine if connected, or uses `pactl` without starting
an engine.

Routing is saved in the configuration. Volume changes apply immediately;
source or route changes require an engine restart.

## Run it locally

You need Python 3.10+, the Python venv module, GTK4/libadwaita and PyGObject
development packages, GCC, make, and a locally installed OBS Studio Flatpak
with its OBSVkCapture extension. The standalone Clipper Flatpak bundles its
own recording stack.

From the repository root:

```bash
python3 -m venv --system-site-packages venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r ui/requirements.txt
make -C engine/src
./clipper
```

`--system-site-packages` lets the virtual environment use the
distribution-provided PyGObject bindings. The native launcher uses `./venv`
when it can import PyGObject.

Run tests, type checking, and linting from the repository root:

```bash
./venv/bin/python -m pip install pytest "pyright[nodejs]" ruff
./tools/run_pytest_quiet.sh
```

## Manual validation

When changing capture, game detection, audio routing, or setup, check the
affected flow in the installed Flatpak with real desktop and game processes:

- Add a display-capture rule, pick a display, launch the game, and save a clip.
- Check game detection with the affected native Steam, Flatpak Steam, Proton,
  launcher-child, or same-name process setup.
- Check separate tracks with real application and Proton/Wine audio streams.
- Check first-run setup in a clean Flatpak account without OBS Studio installed.

See the [Flatpak documentation](../packaging/flatpak/README.md) for package
checks, permissions, and capture patches.
