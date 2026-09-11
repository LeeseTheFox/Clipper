# Application updates

Clipper checks the public GitHub latest-release API once on every startup and at
most once per day afterward while it remains running. The preference disables
automatic checks; the main menu always offers a manual check. Manual checks show
their progress and an up-to-date result as toasts. An automatic startup check
opens the compact update dialog when the main window is visible, or sends a
desktop notification when Clipper is hidden. Cached release metadata, skipped
versions, notification history and pending restart state survive
foreground/background process handoffs.

Downloads and installation require a user action. Network work runs outside the
GTK loop. An available update appears in a banner or, while hidden, one desktop
notification per version. The update dialog links to the version's release notes.
Download cancellation removes the partial file. Installation cannot be cancelled
from the dialog; Flatpak handles the transaction. Desktop authorization may be
required for system installations.

## Release contract

The release workflow validates the source and bundle before publishing:

- `Clipper-X.Y.Z-x86_64.flatpak`: exact versioned bundle.
- `Clipper-x86_64.flatpak`: identical bundle with a stable download name.
- `update-x86_64.json`: schema 1, version, asset name, size, SHA-256, Flatpak
  app reference and commit from the validated installation.
- `SHA256SUMS`: checksums for both bundle names.

Assets are uploaded to a draft release before it becomes public. Only the newest
stable version is marked latest. Published releases are never overwritten by a
workflow rerun; a failed draft can be retried. Publication is serialized to avoid
concurrent releases racing to change latest.

Permanent links:

- https://github.com/LeeseTheFox/Clipper/releases/latest
- https://github.com/LeeseTheFox/Clipper/releases/latest/download/Clipper-x86_64.flatpak

The updater uses the version-specific URL, not the moving download link. It
accepts only newer stable semantic versions with a manifest matching its app,
architecture and branch. It verifies download size and SHA-256 before installation
and the installed commit before reporting success. Trust comes from HTTPS to the
project's GitHub release assets; checksums are not independent publisher signatures.
A dedicated signing key could be added later without changing the UI.

## Installation and restart

The running sandbox's `/.flatpak-info` is matched to host Flatpak installations
by deployment path. Updates retain the existing installation scope, architecture
and branch. The bundle is staged in the app's private cache, translated through
Flatpak's authoritative instance path, and installed using host Flatpak's
`install --or-update --bundle`. The currently running deployment stays in use
until restart. System and named system installations authorize the exact Flatpak
command through host `pkexec`, allowing the desktop to request administrator
authentication before installation. This is necessary because Flatpak's
`--assumeyes` also disables transaction authentication prompts. Per-user updates
run without elevation; authorization failures never switch installation scope.
Long error details wrap within the compact update dialog.
If Flatpak rejects the transaction, its final error detail is
shown in the dialog and recorded in Clipper's logs.

Restart requires a closed editor and no pending clip saves. Restart starts
immediately after those checks; recording stops and the replay buffer is
cleared. A detached host helper waits for the old Flatpak instance to exit,
then launches the new deployment.
The normal Python foreground/background handoff cannot switch deployments.
New saves and editor loads are blocked until restart or a restart-preparation
failure. An editor that is still loading also blocks restart.

The bundled OBS replay writer reports both completed and failed saves, including
muxer process failures. Save notifications wait for completion, and failed saves
release the pending-save guard so another save can be attempted.

Native development runs expose a message directing users to the Flatpak build.
Existing releases without update manifests cannot be installed by this updater.
Existing users need to install the first updater-enabled release manually once.

## Validation

Run `./tools/run_pytest_quiet.sh` for release parsing, download integrity and
cancellation, installation scope, UI scheduling, notification and publication
tests. Run `./venv/bin/python tools/smoke_update_bundles.py` for actual bundle
replacement and fresh-sandbox relaunch in a disposable user installation. The
smoke test requires system `org.gnome.Platform//50` and does not update Clipper.
Run `./venv/bin/python tools/smoke_replay_saves.py` to check two real failed saves
and a successful retry using the installed engine, temporary configuration and
an empty game scene. It does not change the user's clip folder or settings.
