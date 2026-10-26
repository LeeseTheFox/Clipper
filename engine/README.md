# Clipper engine

`engine/src/clipper_engine.c` is the native C process that runs Clipper's OBS/libobs recording work without an OBS Studio window. The GTK UI starts and stops it as needed, then talks to it over newline-delimited JSON on a Unix socket.

## What it handles

- Starts libobs and the OBS plugins packaged with Clipper.
- Creates `pipewire-screen-capture-source` for `display_capture` and `vkcapture-source` for `game_capture`.
- Starts, stops, and saves recent gameplay. It emits status changes and `clip_saved` events, and can provide low-resolution preview frames.
- Stores a PipeWire portal restore token when the portal returns one.
- Reports available encoders, formats, devices, capture modes, and audio sources to the UI.
- Routes one mixed track or up to six separate tracks, including microphone and runtime PipeWire application-audio sources.

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

For native development, the Makefile and launcher use the locally installed OBS Studio Flatpak runtime. The standalone Clipper Flatpak builds and installs its own OBS/libobs stack; see [the Flatpak notes](../packaging/flatpak/README.md).

For focused debugging, the engine also accepts `--socket`, `--output-dir`, `--max-time`, `--capture-mode`, and `--verbose`. Use the UI for the normal lifecycle.

## IPC

The common commands are `get_status`, `get_capabilities`, `get_audio_capabilities`, `list_audio_sources`, `start_replay_buffer`, `stop_replay_buffer`, `save_replay_buffer`, `update_audio_volumes`, `get_preview_frame`, and `shutdown`.

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

## Validate changes

Rebuild after changing engine code:

```bash
make -C engine/src
```

Then run `./tools/run_pytest_quiet.sh`. For the packaged engine, run `./tools/flatpak_smoke_test.py`. It checks a real display-capture clip save and confirms that bundled plugins load.
