# Clipper engine

`engine/src/clipper_engine.c` runs capture, encoding, and clip saving through
OBS/libobs. The GTK UI starts and stops the engine and communicates with it
using newline-delimited JSON over a Unix socket.

## Responsibilities

- Load libobs and the OBS plugins packaged with Clipper.
- Use `pipewire-screen-capture-source` for `display_capture` and
  `vkcapture-source` for `game_capture`.
- Start and stop the replay buffer, save clips, and send status changes and
  `clip_saved` events. Supply low-resolution preview frames on request.
- Store a PipeWire portal restore token when the portal returns one.
- Report available encoders, formats, devices, capture modes, and audio sources.
- Route audio to one mixed track or up to six separate tracks, including
  microphone and PipeWire application audio.

Configuration comes from `$XDG_CONFIG_HOME/clipper/config.json`, or
`~/.config/clipper/config.json` if `XDG_CONFIG_HOME` is unset. The UI selects the
capture mode with `--capture-mode display_capture` or `--capture-mode game_capture`.

## Build and run

From the repository root:

```bash
make -C engine/src
./clipper
```

Native builds use the locally installed OBS Studio Flatpak runtime. The
standalone Clipper Flatpak includes its own OBS/libobs build; see the
[Flatpak build instructions](../packaging/flatpak/README.md#build-it).

For debugging, the engine accepts `--socket`, `--output-dir`, `--max-time`,
`--capture-mode`, and `--verbose`. Normally, the UI manages the engine process.

## IPC

Common commands:

- State and capabilities: `get_status`, `get_capabilities`,
  `get_audio_capabilities`, `list_audio_sources`.
- Recording: `start_replay_buffer`, `stop_replay_buffer`, `save_replay_buffer`.
- Audio and preview: `update_audio_volumes`, `get_preview_frame`.
- Process exit: `shutdown`.

Each response contains `ok`. The engine also sends asynchronous events,
including `status_changed` and `clip_saved`.

## Validation

After changing engine code, rebuild with `make -C engine/src`, then run
`./tools/run_pytest_quiet.sh` from the repository root.

For the packaged engine, run `./tools/flatpak_smoke_test.py`. It checks that
bundled plugins load and that the engine can save a display-capture clip.
