# Building Clipper as a Flatpak

This directory holds the standalone Flatpak package for Clipper.

## Files here

- `io.github.leesethefox.Clipper.yml` is the manifest.
- `io.github.leesethefox.Clipper.desktop` is the exported desktop entry.
- `io.github.leesethefox.Clipper.metainfo.xml` is the AppStream metadata.
- `lint-exceptions.json` records the intentional `finish-args-flatpak-spawn-access` exception.

## Check the package quickly

These checks do not compile OBS/libobs:

```bash
./tools/run_pytest_quiet.sh tests/test_flatpak_metadata.py
desktop-file-validate packaging/flatpak/io.github.leesethefox.Clipper.desktop
appstreamcli validate --no-net packaging/flatpak/io.github.leesethefox.Clipper.metainfo.xml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder --exceptions --user-exceptions packaging/flatpak/lint-exceptions.json manifest packaging/flatpak/io.github.leesethefox.Clipper.yml
```

To check an installed local build without rebuilding it:

```bash
./tools/flatpak_smoke_test.py
```

The smoke test checks installed metadata, starts the packaged engine, saves a display-capture clip, and verifies host-monitor IPC through `flatpak-spawn --host`. Add `--isolated-data` when you want temporary HOME and XDG data directories.

## Build it

OBS/libobs builds use substantial memory and disk. Always use the bounded wrapper from the repository root:

```bash
./tools/run_flatpak_build_quiet.sh
```

It limits the build to 6 GiB of memory, disables swap, and uses one job. It reuses the Flatpak Builder cache, downloading only missing pinned sources. The first build can therefore need network access.

Do not commit local build output. `.gitignore` excludes `build-dir/`, `build-dir-bounded/`, `build-dir-download/`, `repo/`, and `.flatpak-builder/`.

## Why Clipper talks to Flatpak

The manifest requests `--talk-name=org.freedesktop.Flatpak` so Clipper can run its own fixed host-monitor helper with `flatpak-spawn --host`. Flatpak hides the host process list, and no XDG portal lets an app watch an allowlist of host processes.

The helper only checks processes, drives its monitor service, and handles the explicit Steam restart flow. It does not run as root, accept shell snippets or arbitrary commands, launch games, modify Steam launch options, or inject into games.

## Bundled game capture

The package builds x86_64 and i386 Vulkan/OpenGL hooks and the OBS receiver from
the same pinned obs-vkcapture revision. Only the hooks use a GLIBC 2.17 build
sysroot; its RPMs and libraries are build inputs, not installed dependencies.
The receiver uses a per-user Clipper socket so an OBS installation cannot take
its connection. The build validates ELF architecture, ABI imports, library
paths, hashes, licenses, and both architectures' ability to load the hooks.

At runtime Clipper copies the small payload to
`~/.var/app/io.github.leesethefox.Clipper/data/game-capture`, activating updates
atomically. Flatpak Steam gets read access to this directory through one
per-user filesystem override, which applies to user and system Steam installs.
This uses the existing Flatpak host permission; it installs no extension,
downloads nothing, and preserves other overrides. Steam must restart to see a
new permission and to safely update its per-account launch options.

The intentional linter exceptions are `finish-args-flatpak-spawn-access`,
`finish-args-flatpak-appdata-folder-com.valvesoftware.Steam-.local-share-Steam-rw-access`,
and `finish-args-unnecessary-xdg-data-Steam-rw-access`. Steam data access is used
for discovery and atomic launch-option writes, including Flatpak's private
Steam root. `~/.steam` also covers Debian's native client layout.

Flatpak retains application data on a normal uninstall. Choosing to delete
Clipper's data (or using `flatpak uninstall --delete-data
io.github.leesethefox.Clipper`) removes the wrapper and every exported hook.
The launch option falls through to the game when the wrapper is absent.
Flatpak has no application uninstall hook: a Steam filesystem override may
remain, but points only to the removed directory. Removing a game in Clipper
restores its launch options; later user edits are preserved when the exact
Clipper prefix can be removed safely. Older payload versions are retained so
already-running games can continue loading their libraries.

Native glibc Linux and Flatpak Steam are the supported game-capture targets.
Unsupported third-party sandboxes (including Steam Snap) should use display capture.
ARM, native musl games, and anti-cheat-restricted injection are not covered by
the x86_64/i386 payload.

For real frame verification, compile `engine/gamecapture/frame_harness.c` with
`-lGL -lX11 -lvulkan`, then run
`./venv/bin/python tools/game_capture_frame_smoke.py /absolute/path/to/harness --api vulkan`
(or `--api opengl`). The test uses the installed Clipper receiver, saves a clip,
and checks decoded frames change. `--steam-runtime /path/to/_v2-entry-point`
tests Steam's container runtime; `--sandbox` additionally installs a temporary
local test Flatpak and removes it afterward. These optional tests require a
graphical session, ffmpeg, and locally installed Freedesktop 25.08 runtime/SDK
for the sandbox mode. They do not change Steam or Clipper configuration.

## Before a Flathub submission

- Make the project URL in AppStream metadata publicly reachable.
- Add screenshots captured from the real installed Flatpak.
- Run the manifest and repository lints after the final build.
- Confirm that Clipper and bundled third-party licenses are installed in the package.
