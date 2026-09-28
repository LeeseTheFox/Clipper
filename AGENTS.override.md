# AGENTS.override.md

## Purpose

This file gives coding agents the complete repo-specific and personal workflow
instructions needed to work safely in Clipper.

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

## Long-Term Test Policy

- Read `tests/README.md` before adding or changing tests. Keep only tests with
  a durable regression-protection purpose, not tests added merely because code
  changed or to increase test count or coverage percentages.
- Before adding a case, identify the plausible production failure it catches,
  its user-visible consequence or operational risk, and why existing tests do
  not already catch it. Make that purpose clear in the test name; add a short
  comment when the protected invariant is not obvious.
- Exercise production behavior and assert meaningful outcomes. Prioritize data
  integrity, capture/export correctness, security, platform contracts, error
  recovery, resource ownership and asynchronous lifecycle ordering.
- Extend an existing behavioral test when it already covers the same contract.
  Keep separate cases or parameters only for distinct failure modes, boundaries
  or supported platform/protocol variants; avoid multiplying equivalent inputs.
- Do not add source-substring snapshots, tests of copied test-side algorithms,
  assertions that only confirm mock setup, trivial constant/getter checks, or
  duplicates of lint/type checks. Mock dependencies, not the behavior being
  verified.
- Do not freeze incidental wording, widget hierarchy, CSS values, private helper
  structure or current dependency versions. Assert the stable contract instead.
  Static checks are appropriate for explicit repository-wide policies such as
  gettext, icon usage, restricted permissions and pinned downloads, but must
  enforce the policy rather than repeat today's implementation.
- Temporary diagnostic tests do not belong in the permanent suite unless they
  meet these criteria. When reviewing tests in scope, remove or consolidate
  redundant or obsolete cases while preserving distinct regression protection.
  Never remove a meaningful test merely because it fails or to reach a count.
- Use the smallest reliable test that detects the real failure. Do not claim
  mocked or source-level checks prove rendering, GPU or packaging behavior;
  use the relevant integration, smoke or manual check when required.
- Update the purpose map in `tests/README.md` when adding/removing a test module
  or materially changing its coverage. In the handoff, explain the distinct
  protection added or why removed coverage was redundant or obsolete.

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
- When a remote build or release workflow is triggered, do not wait for
  completion or start a long-running sleep. Confirm dispatch (and optionally
  check its initial status once), then wrap up the response and tell the user
  to check whether the Action build succeeds and report back if there are any
  problems.

## Startup Benchmark

- Benchmark the installed Flatpak with `./tools/run_startup_benchmark.sh`.
- It prints compact GTK first-frame submission and clips-ready timings for a
  process-cold desktop-entry launch and a verified normal close-to-tray
  StatusNotifier handoff. It does not measure compositor-visible window time.

## Change Expectations

- Reuse before you build. Analyze existing code and its extension points first.
  When an existing element fits, extend it with a builder method instead of
  creating a sibling implementation.
- Do not create documentation unless it is necessary for the requested change
  or the user explicitly asks for it.
- If you change Python behavior under `ui/` or `tests/`, run the Python test suite before finishing.
- If you change engine code under `engine/src/`, rebuild the engine before finishing.
- After adding a feature or fixing a bug, always build and install the Flatpak
  unless I say otherwise. Run `./tools/run_flatpak_build_quiet.sh`, then
  `./tools/install_flatpak_quiet.sh` to force-close the installed Clipper app
  and quietly reinstall the latest build from the local `clipper-local`
  repository. This is standing user consent and does not require additional
  confirmation.
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

## Git remotes and publishing

- Treat `origin` (GitLab) as the default fetch and push destination.
- Treat `github` as Clipper's public release remote.
- Do not push anything to `github` unless I explicitly request a GitHub push,
  publication, or release in the current conversation.
- Normal development branches and unfinished work belong on GitLab only.
- When publishing to GitHub, push only the explicitly requested release-ready
  branch and version tags; never mirror every ref or every tag automatically.
- Before pushing to GitHub, verify the working tree, destination remote, and
  exact refs that will be published.

## Versioned releases

When I explicitly request a **version bump and push**, treat it as authorization
to publish that release to both GitLab and GitHub. Do not infer this
authorization from an ordinary version bump, build, commit, or GitLab push.

- Update these four Clipper application-version locations to the requested
  `X.Y.Z` version:
  - `pyproject.toml`: the `[project]` `version` field.
  - `ui/main.py`: `Adw.AboutWindow(version=...)` in `on_about`.
  - `engine/src/clipper_engine.c`: the `ENGINE_VERSION` macro reported through
    engine IPC.
  - `packaging/flatpak/io.github.leesethefox.Clipper.metainfo.xml`: prepend a
    new current `<release version="X.Y.Z" date="YYYY-MM-DD">` entry to the
    AppStream release history; retain older entries unchanged.
- Do not alter similarly named values that are not Clipper's release version,
  including Flatpak runtime and dependency versions, XML or license versions,
  engine/UI data-schema and protocol versions, or vendored third-party code.
- Use the requested semantic version as `X.Y.Z` in metadata and create an
  **annotated** Git tag named `vX.Y.Z` on the release commit. The tag and every
  version-bearing Clipper file must describe the same release.
- Review the complete diff and working tree before committing. Keep unrelated
  user changes out of the release commit.
- Run the relevant validation and a Flatpak packaging build before publishing.
- Before every push, verify the exact release-ready branch, tag target, and
  `origin` (GitLab) and `github` remotes. Push only that branch and the new
  version tag to both remotes; do not mirror other branches or tags.
- Pushing the version tag to `github` starts the public release workflow. Treat
  that tag push as publication: validate the workflow and exact refs first.
- After the GitHub push, confirm the release workflow was dispatched if its
  status is available, then wrap up instead of waiting for completion. Tell the
  user to check whether the Action build succeeds and report back if there are
  any problems.
- Do not move a tag after its GitHub Release has been published unless I
  explicitly request replacing that release. A failed run may be corrected and
  retagged only while it has not created a GitHub Release.

## Working Directory

- Run commands from the repository root.

## Environment

- Use the local virtual environment for Python tooling. Do not use the system Python.
- Virtual environment path: `./venv`
- Python version target: 3.10+
- Installing additional packages into `./venv` is allowed when it helps testing, linting, or type checking.
