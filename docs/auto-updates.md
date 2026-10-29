# Application updates

Clipper checks GitHub's latest-release API at startup, then at most once per
day while running. Users can disable automatic checks in Preferences or check
manually from the main menu. Manual checks show progress and report when
Clipper is up to date.

An available update appears in a banner. An automatic startup check opens the
update dialog if the main window is visible; when hidden, Clipper sends one
desktop notification per version. The dialog links to the release notes.
Cached release details, skipped versions, notification history, and pending
restart state survive foreground/background process handoffs.

Downloads and installation require a user action. Network work runs outside
the GTK loop. Cancelling a download removes the partial file. Once Flatpak
starts installation, it cannot be cancelled from the dialog. System
installations may require desktop authorization.

## Release files

The release workflow validates the source and bundle before publishing:

| Asset | Contents |
| --- | --- |
| `Clipper-X.Y.Z-x86_64.flatpak` | Versioned application bundle. |
| `Clipper-x86_64.flatpak` | The same bundle with a stable download name. |
| `update-x86_64.json` | Schema 1 manifest: version, asset name, size, SHA-256, Flatpak app reference, and commit from the validated installation. |
| `SHA256SUMS` | Checksums for both bundle names. |

Assets are uploaded to a draft release before publication. Only the newest
stable version is marked latest. A workflow rerun can retry a failed draft,
but cannot overwrite a published release. Publication runs serially to prevent
releases from racing to change latest.

Permanent links:

- [Latest release](https://github.com/LeeseTheFox/Clipper/releases/latest)
- [Latest Flatpak bundle](https://github.com/LeeseTheFox/Clipper/releases/latest/download/Clipper-x86_64.flatpak)

The updater downloads from the version-specific URL. It accepts only newer
stable semantic versions whose manifests match the app, architecture, and
branch. It checks download size and SHA-256 before installation, then verifies
the installed commit before reporting success. Downloads rely on HTTPS to the
project's GitHub release assets. Checksums are not independent publisher signatures.

## Installation

The updater matches the running sandbox's `/.flatpak-info` to a host Flatpak
installation by deployment path. It preserves the installation scope,
architecture, and branch.

The bundle is staged in the app's private cache. The updater resolves its host
path through Flatpak's instance path and runs host Flatpak's
`install --or-update --bundle`. The current process keeps using the old
deployment until restart.

System installations, including named ones, authorize the exact Flatpak command
through host `pkexec`. This allows the desktop to request administrator
authentication: Flatpak's `--assumeyes` disables transaction authentication
prompts. Per-user updates run without elevation. Authorization failures never
switch installation scope.

If Flatpak rejects installation, the dialog shows its final error detail and
Clipper records it in the logs. Long errors wrap within the dialog.

## Restart

Restart requires a closed editor, no editor load in progress, and no pending
clip saves. Once these checks pass, recording stops and the replay buffer is
cleared. A detached host helper waits for the old Flatpak instance to exit,
then launches the new deployment. A normal Python foreground/background handoff
cannot switch deployments.

New saves and editor loads remain blocked until restart or a failure during
restart preparation.

The bundled OBS replay writer reports completed and failed saves, including
muxer process failures. Save notifications wait for completion. A failed save
releases the pending-save guard so the user can try again.

Native development runs direct users to the Flatpak build. The updater cannot
install releases that lack an update manifest.

## Validation

Run these commands from the repository root:

- `./tools/run_pytest_quiet.sh` checks release parsing, download integrity and
  cancellation, installation scope, UI scheduling, notifications, and publication.
- `./venv/bin/python tools/smoke_update_bundles.py` checks bundle replacement
  and relaunch in a disposable user installation. It requires system
  `org.gnome.Platform//50` and does not update the user's Clipper installation.
- `./venv/bin/python tools/smoke_replay_saves.py` checks two failed saves and a
  successful retry with the installed engine. It uses temporary configuration
  and an empty game scene, leaving the user's clip folder and settings unchanged.
