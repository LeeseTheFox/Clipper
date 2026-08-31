# Clipper engine

`engine/src/clipper_engine.c` is the native C process that runs Clipper's OBS/libobs recording work without an OBS Studio window. The GTK UI starts and stops it as needed, then talks to it over newline-delimited JSON on a Unix socket.

## What it handles

- Starts libobs and the OBS plugins packaged with Clipper.
- Creates `pipewire-screen-capture-source` for `display_capture` and `vkcapture-source` for `game_capture`.
- Starts, stops, and saves recent gameplay. It emits status changes and `clip_saved` events, and can provide low-resolution preview frames.
- Stores a PipeWire portal restore token when the portal returns one.
- Reports available encoders, formats, devices, capture modes, and audio sources to the UI.
- Routes one mixed track or up to six separate tracks, including microphone and runtime PipeWire application-audio sources.

The engine reads `$XDG_CONFIG_HOME/clipper/config.json`, falling back to `~/.config/clipper/config.json`. The UI normally starts it with either `--capture-mode display_capture` or `--capture-mode game_capture`.

## Build and run

From the repository root:

```bash
make -C engine/src
./clipper
```

For native development, the Makefile and launcher use the locally installed OBS Studio Flatpak runtime. The standalone Clipper Flatpak builds and installs its own OBS/libobs stack; see [the Flatpak notes](../packaging/flatpak/README.md).

For focused debugging, the engine also accepts `--socket`, `--output-dir`, `--max-time`, `--capture-mode`, and `--verbose`. Use the UI for the normal lifecycle.

## IPC

The common commands are `get_status`, `get_capabilities`, `get_audio_capabilities`, `list_audio_sources`, `start_replay_buffer`, `stop_replay_buffer`, `save_replay_buffer`, `update_audio_volumes`, `get_preview_frame`, and `shutdown`.

Each response contains `ok`. Asynchronous events include `status_changed` and `clip_saved`.

## Validate changes

Rebuild after changing engine code:

```bash
make -C engine/src
```

Then run `./tools/run_pytest_quiet.sh`. For the packaged engine, run `./tools/flatpak_smoke_test.py`. It checks a real display-capture clip save and confirms that bundled plugins load.
