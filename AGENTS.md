# AGENTS.md

## Purpose

This file gives coding agents the minimum repo-specific instructions needed to work safely in Clipper.

## Project Layout

- `clipper`: top-level launcher for the engine + UI
- `engine/src`: C engine and build files
- `ui`: Python GTK/libadwaita application code
- `tests`: automated Python test suite
- `docs`: supporting notes and research

## Build Commands

- Build the engine: `make -C engine/src`
- For a full, resource-bounded Flatpak build, use this exact command from the
  repository root:

```bash
./tools/run_flatpak_build_quiet.sh
```

- The Flatpak build wrapper captures verbose compiler output, prints a compact
  result on success, and shows limited failure context when the build fails.
- The wrapper reuses the Flatpak Builder cache and automatically downloads any
  missing pinned manifest sources without refreshing cached VCS sources.
  Network access is therefore required for the first build or
  after adding a new source.

## Test Commands

- For agent-friendly test and type-check output, always use this exact command from the repo root:

```bash
./tools/run_pytest_quiet.sh
```

- This script runs pytest, Pyright, and Ruff, prints compact summaries, and shows any failures with their reasons. It is the preferred method for validating Python changes.
- Run it without arguments so the complete test suite runs immediately. Optional pytest targets are available for focused iteration, but usually there's no point in using them if you intend to run the full suite afterward anyways.

## Release Automation

- Pushing a tag matching `v*` to GitHub starts `.github/workflows/release.yml`.
- The workflow requires an annotated semantic-version tag, runs the complete
  test/type/lint suite in the pinned GNOME SDK, checks every application-version
  location with `tools/validate_release_version.py`, builds the Flatpak bundle,
  and creates or updates the corresponding GitHub Release.
- `tools/generate_release_notes.py` builds categorized release notes from
  Conventional Commit subjects. Keep commit subjects accurate because they
  become public changelog entries.
- A successful release is titled `Clipper X.Y.Z` and includes a
  `Clipper-X.Y.Z-x86_64.flatpak` asset.

## Startup Benchmark

- Benchmark the installed Flatpak with `./tools/run_startup_benchmark.sh`.
- It prints compact GTK first-frame submission and clips-ready timings for a
  process-cold desktop-entry launch and a verified normal close-to-tray
  StatusNotifier handoff. It does not measure compositor-visible window time.

## Change Expectations

- If you change Python behavior under `ui/` or `tests/`, run the Python test suite before finishing.
- If you change engine code under `engine/src/`, rebuild the engine before finishing.
- After adding a feature or fixing a bug, build the Flatpak when the change warrants a packaging check, using `./tools/run_flatpak_build_quiet.sh` from the repository root.
- Do not install or reinstall the Clipper Flatpak automatically. Before running `./tools/install_flatpak_quiet.sh`, obtain explicit user consent or follow an explicit user direction.
- Do not add new project/runtime dependencies unless the task requires them.

## Commit Messages

- Keep each commit focused on one logical change. Do not combine unrelated
  changes merely because they were made together.
- Use the Conventional Commits-style subject format
  `<type>[optional scope]: <imperative summary>`. Use the existing types
  consistently: `feat`, `fix`, `docs`, `i18n`, `refactor`, `perf`, `test`,
  `build`, `ci`, and `chore`.
- Start the summary with a lowercase imperative verb, describe the resulting
  change, omit the final period, and keep it at 50 characters or fewer when
  practical. For example: `fix(editor): preserve export settings`.
- Add a body after a blank line when the subject alone does not make the
  motivation, previous behavior, or important trade-offs clear. Wrap body
  text at about 72 characters.
- Use commit trailers only when useful, such as `Fixes: #123` or
  `BREAKING CHANGE: <description>`. Clearly mark every breaking change.

## Icon Policy

- Use symbolic icons from the GNOME Icon Development Kit packaged in Clipper's
  `icon-development-kit.gresource` for new interface actions and categories.
- Search for suitable names in the **Icon Library** app. Choose icons from its
  regular categorized collection, not the **Pre-Installed System Icons**
  section, because pre-installed names can come from the user's host theme and
  are not guaranteed inside the Flatpak.
- To search the exact collection packaged by an installed Clipper Flatpak, run:

```bash
flatpak run --command=sh io.github.leesethefox.Clipper -c \
  'gresource list /app/share/clipper/ui/icon-development-kit.gresource' | \
  sed -E 's#.*/##; s#\.svg$##' | sort | rg -i 'SEARCH TERM'
```

- Reference every interface icon through a constant in `ui/icon_names.py`;
  never hard-code icon names at GTK call sites.
- Prefer `-symbolic` icons so GTK can apply the current theme color.
- Do not vendor a complete icon pack, bulk SVG collection, or copied Icon Library
  assets in the repository. The Flatpak manifest already
  downloads the pinned Icon Library release and compiles its complete icon set
  into one GResource, so new icons normally require only a constant and usage.
- Delete actions intentionally use `xsi-user-trash-symbolic` from XApp Symbolic
  Icons. The Flatpak manifest downloads its pinned upstream release but installs
  only that SVG and its license notices; do not assume any other `xsi-*` icon is
  available without packaging it explicitly.
- The only repository-owned icon assets are Clipper's application icon and its
  dedicated symbolic tray icon under `ui/icons/hicolor`. Preserve the tray icon
  name `io.github.leesethefox.Clipper-symbolic`; StatusNotifier hosts use
  that exported name, and replacing it with a generic theme icon loses
  Clipper's tray identity.

## UI Text Conventions

- Every user-facing string must use the gettext helpers from `ui/i18n.py`;
  never add hard-coded UI text. This includes windows, setup,
  preferences, dialogs, toasts, notifications, tray menus and tooltips,
  accessibility labels, and desktop-portal prompts.
- Add or change UI text in every available language in the same change. Run
  `./venv/bin/python tools/update_translations.py`, then translate every new or
  changed entry in every `po/*.po` catalog. Incomplete catalogs must not be
  committed.
- Keep machine-readable values, action names, protocol fields, and persisted
  configuration independent from translated display labels. UI behavior must
  never compare or branch on translated text.
- Use `ngettext`/`npgettext` for count-dependent text and named `%()`
  placeholders for interpolated values so translators can reorder them.
- New languages are added as `po/<locale>.po`; follow `po/README.md` and do not
  add per-language application code.
- All user-facing text should use sentence case (only the first word capitalized), not title case (Every Word Capitalized).
- This applies to buttons, labels, menu items, dialog titles, status messages, tooltips, placeholders, and all other UI text.
- Keep proper nouns capitalized (Steam, Clipper, PipeWire, etc.) and acronyms unchanged (API, UI, PID, etc.).
- Examples:
  - ✓ "Add Steam game"
  - ✗ "Add Steam Game"
  - ✓ "Show logs"
  - ✗ "Show Logs"
  - ✓ "No games added"
  - ✗ "No Games Added"

## Keyboard Shortcuts

- All letter, number, and punctuation shortcuts must match the physical key's
  base-layout value through its hardware keycode. Never compare only the
  translated `keyval`, because shortcuts must keep working when the user
  switches to a different keyboard or input layout.
- Add an automated test using a non-Latin translated key value whenever a new
  keyboard shortcut is introduced.

## Notes

- `pyproject.toml` configures pytest for this repo.
- The automated pytest suite lives under `tests/`. Files in `ui/` named `test_*.py` are helper scripts, not part of the main pytest suite.
