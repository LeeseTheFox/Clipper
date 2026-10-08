# Reproducible README screenshots

From a fresh checkout on x86_64 Linux with Flatpak and Python 3.10+ (including
its venv module), run:

```sh
./tools/generate_screenshots.sh
```

The script creates a local virtual environment if needed. It reuses the exact
GNOME SDK commit in `environment.json` when available, otherwise downloads it
into a separate Flatpak installation under the user's cache. It downloads and
verifies the icon archives and VDF wheel pinned by Clipper's Flatpak manifest,
plus the Noto Sans fonts pinned in `environment.json`. Fonts stay in the user's
cache rather than the repository or application package. Network access is
needed for these first-run downloads.
No installed Clipper, Steam library, games, clips, audio devices, display server,
or app configuration is required.

The generator captures the current checkout's actual GTK views in every
`po/*.po` language, using both light and dark themes. The four scenes are Games,
Audio, Clips, and the video editor. It replaces `assets/screenshots` images and
makes each localized README gallery use dark screenshots, retaining
its translated alt text. All captures finish successfully before files are
replaced. An alternate output directory leaves READMEs unchanged:

```sh
./tools/generate_screenshots.sh --output-dir /tmp/clipper-screenshots
./tools/generate_screenshots.sh --check
```

`--check` renders again and compares every PNG byte and README reference. The
SDK commit, software Cairo renderer, pinned Noto Sans 10 font and runtime
fallback fonts, DPI, scale, theme, decoration layout, locale, time zone, fixed
clock, and fixture
checksums define the reproducibility contract. GTK rendering runs on a private
Broadway display and D-Bus session without network, audio, GPU, or host desktop
access. Config, media stand-ins, catalogs and caches live in disposable
directories. The generator constructs Clipper's actual windows without starting
the recording engine, desktop integration or host app session.

Window sizes come directly from the current UI constructors for every tab,
including Clips. The generator imports `MainWindow`, its page widgets,
`EditorWindow`, and `EditorPreview` from this checkout's `ui/` directory. It
adds no screenshot-only layout, CSS, widgets or font-weight rules. UI changes
there appear in the next screenshots. The editor inherits its production
preview widgets and replaces only their decoder backend with the saved frame.
The fixed Noto Sans 10 desktop font setting matches the original app captures;
normal runtime Fontconfig rules handle font matching and weights.

## Fixture format

`scene.json` contains the four whitelist games and their original displayed
locations and capture modes, configured microphone and four audio routes,
five example clips' names, sizes, timestamps and durations, and the saved
`THE_FINALS_2026-07-26_16-20-32` editor project. Private Steam account identifiers,
portal tokens, managed launch options and undo history are unnecessary and are
excluded. Game icons and banners are stored once, shared across all captures.

Only five small thumbnails and one editor frame are retained; no videos or
audio recordings are committed. The gallery scans temporary sparse files with
the recorded metadata and reads its regular prefilled thumbnail/duration cache.
Audio discovery returns the recorded device/application identities. The editor
uses a still decoder output and recorded waveform peaks while its real timeline
and project model render the saved cuts, mute states and gains.

The editor playhead is exactly 2,000,000 microseconds into the edited timeline,
with 225% zoom. The saved cuts map that point to 4,483,383 microseconds in the
source clip; `editor-frame.jpg` was extracted there. The third segment is
selected. Captures use the current app's default window sizes. All five clips
remain in the gallery, with scrolling and visible rows determined by the
production view. All four audio tracks remain in the editor project; the visible
lanes follow its normal viewport. `waveforms.bin.z` stores the
production waveform analyzer's 20 ms peak buckets as zlib-compressed,
little-endian signed 16-bit min/max pairs, in source track order. The bucket
counts are in `scene.json`.

When intentionally replacing fixture assets, update their SHA-256 entries in
`environment.json`, retain the same source-frame/playhead mapping, regenerate
all captures, inspect each scene, then run `--check`. Change the SDK commit only
as a deliberate rendering-environment update. These fixture assets serve the
README screenshot examples, not media playback or export tests.
