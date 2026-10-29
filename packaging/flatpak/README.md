# Building Clipper as a Flatpak

This directory contains the manifest and metadata for Clipper’s standalone
Flatpak package.

## Files

- `io.github.leesethefox.Clipper.yml` is the manifest.
- `io.github.leesethefox.Clipper.desktop` is the exported desktop entry.
- `io.github.leesethefox.Clipper.metainfo.xml` is the AppStream metadata.
- `lint-exceptions.json` lists approved Flatpak linter exceptions.

## Package checks

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

The smoke test checks installed metadata, starts the packaged engine, saves a
display-capture clip, and checks host-monitor IPC through `flatpak-spawn --host`.
Use `--isolated-data` to run with temporary HOME and XDG data directories.

## Build it

Install Flatpak Builder and the GNOME 51 SDK:

```bash
flatpak install --user flathub org.gnome.Sdk//51 org.flatpak.Builder
```

The build wrapper also requires `systemd-run`.

OBS/libobs builds use substantial memory and disk. From the repository root,
use the build wrapper to limit resource use:

```bash
./tools/run_flatpak_build_quiet.sh
```

The wrapper limits the build to 6 GiB of memory, disables swap, and uses one
job. It reuses the Flatpak Builder cache and downloads missing pinned sources.
The first build requires network access.

Keep build output out of commits. `.gitignore` excludes `build-dir/`,
`build-dir-bounded/`, `build-dir-download/`, `repo/`, and `.flatpak-builder/`.

## Install a local build

Install the completed build with:

```bash
./tools/install_flatpak_quiet.sh
```

The installer prepares the bundle before closing Clipper. Both local builds
and GitHub releases are installed as bundles; Clipper’s updater handles release
updates. The local `repo/` directory is only build output.

If the installed app still has a repository origin, the installer migrates it
once through a normal uninstall and bundle install. It retains application data,
including settings, editor drafts and exported game-capture hooks. Subsequent
installs replace the bundle without repeating the migration.

## Host access

The manifest requests `--talk-name=org.freedesktop.Flatpak` to run Clipper’s
host-monitor helper through `flatpak-spawn --host`. Flatpak hides host processes,
and no XDG portal supports watching the configured games. This helper lets
Clipper detect when those games start and stop.

The helper checks processes, manages the monitor service, and handles requested
Steam restarts. It runs without root privileges and accepts only fixed
operations. It cannot run arbitrary commands or shell snippets, launch games,
change Steam launch options, or inject into games.

Deleting clips normally uses the desktop trash portal. If it returns a generic
failure for a regular clip, Clipper uses the existing host access to try the
host's `gio trash` utility. The fallback verifies the file's device and inode
before trashing, preserves the host trash location and directory permissions,
and stops with Clipper's bus connection. If the utility is unavailable or the
fallback fails, Clipper reports the failure and retains the clip's editor data.

## Bundled game capture

The package builds x86_64 and i386 Vulkan/OpenGL hooks and the OBS receiver from
the same pinned obs-vkcapture revision. Only the hooks use a GLIBC 2.17 build
sysroot; its RPMs and libraries are build inputs, not installed dependencies.
The receiver uses a per-user Clipper socket to avoid connection conflicts
with OBS. The build validates ELF architecture, ABI imports, library
paths, hashes, licenses, and both architectures' ability to load the hooks.

At runtime Clipper copies the game-capture files to
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
Use Display capture for other sandboxes, including Steam Snap.
ARM, native musl games, and anti-cheat-restricted injection are not covered by
the x86_64/i386 payload.

## Check captured frames

To verify captured frames, compile `engine/gamecapture/frame_harness.c` with
`-lGL -lX11 -lvulkan`, then run
`./venv/bin/python tools/game_capture_frame_smoke.py /absolute/path/to/harness --api vulkan`
(or `--api opengl`). The test uses the installed Clipper receiver, saves a clip,
and checks decoded frames change. `--steam-runtime /path/to/_v2-entry-point`
tests Steam's container runtime; `--sandbox` additionally installs a temporary
local test Flatpak and removes it afterward. These optional tests require a
graphical session, ffmpeg, and locally installed Freedesktop 25.08 runtime/SDK
for the sandbox mode. They do not change Steam or Clipper configuration.

## Maintaining the capture patches

The manifest orders patches against pinned OBS and obs-vkcapture sources; keep
that order when rebasing. The direct-surface patch extends internal OBS encoder
interfaces, so rebuild all bundled modules together. These patches are not
compatible with an arbitrary host OBS installation.

- VAAPI import caches retain graphics wrappers and their owning FFmpeg frames
  context, never frame-buffer leases. FFmpeg decides when a surface is reusable.
  Caches are bounded to 32 entries and use transient imports at capacity.
- Direct conversion writes NV12 into leased encoder surfaces. Graphics precedes
  the queue mutex; each queued lease retains its cache and a weak encoder identity.
  GL completion fences must finish before encoding or releasing a lease. Destroy
  imported wrappers before releasing their frames context. Unsupported formats,
  consumers or allocation failures retain the ordinary conversion/copy path.
- Native capture skips RGB composition only for an opaque, unfiltered, identity
  game-capture scene at matching SDR dimensions. Preview requests run on the OBS
  graphics task queue and materialize the scene when its RGB target is stale.
- Lazy buffers preserve queue limits and backpressure, allocating fallback GPU
  textures and CPU/readback buffers when first needed. PipeWire imports belong
  to their stream buffers; per-frame synchronization is renewed independently.
- Hook pacing uses `engine/gamecapture/copy_pacing.h`. CPY1 supplies a twice-FPS
  copy budget; CPY2 aligns copies with the consumer's monotonic deadline. Old
  peers retain compatible fixed-size messages. Neither path limits game FPS.

## Troubleshoot capture patches

Set any of these engine environment variables to `0` to disable that
optimization: `CLIPPER_VAAPI_IMPORT_CACHE`,
`CLIPPER_VAAPI_DIRECT_SURFACES`, `CLIPPER_NATIVE_CAPTURE`, and
`CLIPPER_HOOK_COPY_PACING`. The smoke tool exposes matching `--no-...` options.
Native capture requires direct surfaces. Import-cache controls affect the
ordinary copy path. Keep FFmpeg's default encoder queue depth.

GNOME 51 supplies the FFmpeg VAAPI teardown fixes needed for repeated replay
sessions. Its SDK also requires `-fno-link-libatomic` when linking the static host
monitor. Check resource use across repeated engine restarts with:

```bash
./venv/bin/python tools/smoke_replay_saves.py \
  --encoder ffmpeg_vaapi_tex --resolution 1920x1080 --fps 60 \
  --cycles 20 --assert-stable-fds
```

For native capture, compile the frame harness with dimensions matching the output
(for example `-DWIDTH=2560 -DHEIGHT=1440`), then run:

```bash
./venv/bin/python tools/game_capture_frame_smoke.py /absolute/path/to/harness \
  --api opengl --encoder ffmpeg_vaapi_tex --resolution 2560x1440 \
  --source-fps 240 --record-seconds 6 --check-color-chart \
  --require-native-capture --check-preview
```

Run capture checks sequentially because the receiver socket is shared. The
OpenGL chart checks color and orientation; Vulkan checks changing frame IDs.
Native unit tests extract packaged C functions using
`tools/generate_native_test_headers.py` and compile them against driver stubs.
These unit tests check guards and resource lifetimes. Use hardware smoke tests
to verify rendered frames and synchronization. Neither check measures gameplay FPS.
