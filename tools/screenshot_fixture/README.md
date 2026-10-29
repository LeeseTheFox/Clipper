# README screenshots

## Generate screenshots

On x86_64 Linux with Flatpak and Python 3.10+ (including its venv module), run
from the repository root:

```sh
./tools/generate_screenshots.sh
```

The script uses bundled demo data. It needs no installed Clipper, Steam library,
games, clips, audio devices, display server, or app configuration.

It captures the Games, Audio, Clips, and editor views in every `po/*.po`
language, in light and dark themes. After all captures succeed, it replaces
`assets/screenshots` and updates localized README galleries to use dark
screenshots, preserving translated alt text.

To save captures elsewhere without changing the READMEs:

```sh
./tools/generate_screenshots.sh --output-dir /tmp/clipper-screenshots
```

To render again and compare every PNG byte and README image reference:

```sh
./tools/generate_screenshots.sh --check
```

## Environment

The script creates a local virtual environment if needed. It uses the GNOME SDK
commit pinned in `environment.json`, downloading it to a separate Flatpak
installation under the user's cache if unavailable. It also downloads and
verifies the manifest's pinned icon archives and VDF wheel, and the Noto Sans
fonts pinned in `environment.json`. These first-run downloads require network
access. Fonts remain in the user's cache.

Rendering runs on a private Broadway display and D-Bus session without network,
audio, GPU, or host desktop access. Configuration, media stand-ins, catalogs,
and caches use temporary directories. The recording engine and desktop
integration do not start.

Reproducibility depends on the SDK commit, software Cairo renderer, pinned
Noto Sans 10 font and runtime fallback fonts, DPI, scale, theme, decoration
layout, locale, time zone, fixed clock, and fixture checksums.

The generator uses `MainWindow`, its page widgets, `EditorWindow`, and
`EditorPreview` from the current checkout. Window sizes, layout, CSS, and
visible content come from application code. The editor uses its normal preview
widgets with a still-frame decoder. Fontconfig handles fallback fonts and weights.

## Demo data

`scene.json` contains four games with paths and capture modes, a microphone,
four audio routes, five clips with metadata, and the saved
`THE_FINALS_2026-07-26_16-20-32` editor project. It excludes private Steam account
IDs, portal tokens, managed launch options, and undo history. Game icons and
banners are shared across captures.

The fixture stores five thumbnails and one editor frame, without video or audio
recordings. The gallery scans temporary sparse files and uses its normal
thumbnail and duration cache. Audio discovery returns the fixture's device and
application identities. The editor uses saved waveform peaks, cuts, mute states,
and gains with its normal timeline and project model.

The editor starts at 2,000,000 microseconds with 225% zoom and the third segment
selected. The cuts map that position to 4,483,383 microseconds in the source
clip, matching `editor-frame.jpg`. All five clips and four audio tracks remain
loaded; the application's viewport determines which are visible.

`waveforms.bin.z` contains the waveform analyzer's 20 ms peak buckets as
zlib-compressed, little-endian signed 16-bit min/max pairs in source track order.
Bucket counts are in `scene.json`.

## Replace fixture assets

Update the SHA-256 entries in `environment.json` and preserve the mapping
between the editor playhead and source frame. Regenerate all captures, inspect
each scene, then run `--check`. Change the SDK commit only when updating the
rendering environment.

These fixtures are for README screenshots. Playback and export tests need
actual media.
