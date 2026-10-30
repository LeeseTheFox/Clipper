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

The UI normally starts the engine with either `--capture-mode display_capture` or `--capture-mode game_capture`.

## Shared configuration

The UI and engine share `$XDG_CONFIG_HOME/clipper/config.json`, falling back to
`~/.config/clipper/config.json` when `XDG_CONFIG_HOME` is unset or empty. In the
Flatpak these paths belong to the application's sandbox.

The UI owns user preferences. The engine writes `pipewire_restore_token` after
the desktop portal issues a token. Ordinary UI saves reread the file and preserve
the latest nonempty engine-owned token, even when memory holds an older one.
Clearing the display selection and factory reset intentionally bypass that
preservation through the dedicated configuration methods in `ui/config.py`.
Both writers replace the JSON file atomically; this prevents partial reads but
does not serialize simultaneous writes.

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

The wire format is UTF-8 JSON objects separated by newlines. A socket read can
contain part of a message or several messages. Commands contain `cmd`; responses
contain `ok`; asynchronous events contain `event`. Replies arrive in command
order on each connection, without request IDs. The client must reserve a reply
slot even for commands without callbacks. Events may arrive between replies and
must not consume those slots. Engine responses and event broadcasts share the
client write mutex so their bytes cannot interleave within a message.

A successful `save_replay_buffer` response only acknowledges the request.
`clip_saved` or `clip_save_failed` reports its completion. The UI permits one
pending save, waits for completion before notifying the user, and blocks an
update restart while a save is pending. A rejected command releases the pending
save immediately. Keep these rules aligned with `ui/engine_client.py` and the
engine IPC tests when changing either end of the protocol.

## Validation

After changing engine code, rebuild with `make -C engine/src`, then run
`./tools/run_pytest_quiet.sh` from the repository root.

For the packaged engine, run `./tools/flatpak_smoke_test.py`. It checks that
bundled plugins load and that the engine can save a display-capture clip.
