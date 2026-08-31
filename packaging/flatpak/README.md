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

## Before a Flathub submission

- Make the project URL in AppStream metadata publicly reachable.
- Add screenshots captured from the real installed Flatpak.
- Run the manifest and repository lints after the final build.
- Confirm that Clipper and bundled third-party licenses are installed in the package.
