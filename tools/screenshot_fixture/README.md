# README screenshots and banners

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
screenshots, preserving translated alt text. It also creates a localized
`banner.png` from the four dark screenshots and application icon, and places it
above each README heading.

The banner shows Games, Clips, the editor, and Audio from left to right in
diagonal panes. All scenes share the main window’s default size and scale; the
editor is resized to the same allocation. The outer panes are narrower to balance
the composition. Rounded transparent corners, theme colors, constant-width gaps,
and blurred pane shadows are rendered with Cairo and librsvg inside the SDK.

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
verifies the manifest's pinned icon archives and VDF wheel. These first-run
downloads require network access. Adwaita Sans is bundled under `fonts/` with
its copyright notice and SIL Open Font License 1.1; no font download or system
installation is needed.

Rendering runs on a private Broadway display and D-Bus session without network,
audio, GPU, or host desktop access. Configuration, media stand-ins, catalogs,
and caches use temporary directories. The recording engine and desktop
integration do not start.

Reproducibility depends on the SDK commit, software Cairo renderer, pinned
Adwaita Sans 10 pt font and runtime fallback fonts, DPI, scale, theme, decoration
layout, locale, time zone, fixed clock, and fixture checksums.

The generator uses `MainWindow`, its page widgets, `EditorWindow`, and
`EditorPreview` from the current checkout. Window sizes, layout, CSS, and
visible content come from application code. The editor uses its normal preview
widgets with a still-frame decoder. No screenshot-only layout, CSS, widgets, or
font-weight rules are added. Both the UI and banner use the bundled Adwaita Sans
variable font; the UI uses 10 pt and the banner title uses 80 px bold. The font
comes unmodified from [GNOME’s adwaita-fonts 51.0 release][adwaita-fonts]. Its
checksum and license checksum are pinned in `environment.json`. Fontconfig uses
only the bundled font and the pinned SDK’s DejaVu fallback fonts.

## Demo data

`scene.json` contains four games with paths and capture modes, a microphone,
four audio routes, five clips with metadata, and the saved
`THE_FINALS_2026-07-26_16-20-32` editor project. It excludes private Steam account
IDs, portal tokens, managed launch options, and undo history. Game icons and
banners are shared across captures. The original Steam header artwork keeps its
native aspect ratio; production GTK `COVER` cropping fits the thumbnails without
stretching the fixture images.

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

[adwaita-fonts]: https://download.gnome.org/sources/adwaita-fonts/51/adwaita-fonts-51.0.tar.xz
