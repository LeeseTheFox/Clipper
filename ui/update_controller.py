"""GTK update presentation; network and installation work run in a worker."""

import os
import threading
import time
import urllib.error
from dataclasses import asdict
from pathlib import Path
from typing import Any

import updates
from gi.repository import Adw, Gio, GLib, Gtk, Pango
from i18n import _


class UpdateController:
    def __init__(self, app):
        self.app = app
        self.config = app._config
        self.release = None
        self.installation = None
        self.busy = False
        self.installed = False
        self._checking = False
        self._manual_check_requested = False
        self._present_release_requested = False
        cached_release = self.config.get("update_available", {})
        if cached_release:
            try:
                candidate = updates.Release(**cached_release)
                if updates.version_tuple(candidate.version) > updates.version_tuple(
                    updates.current_version()
                ):
                    self.release = candidate
                    self.installed = (
                        self.config.get("update_installed_version", "") == candidate.version
                    )
            except (TypeError, ValueError, OSError):
                pass
        self.cancel = threading.Event()
        self.dialog: Any = None
        self.status: Gtk.Label
        self.button: Gtk.Button
        self.progress: Gtk.ProgressBar
        self.closed = False
        self.timer = GLib.timeout_add_seconds(30, self._tick)
        self.startup_check = GLib.idle_add(self._check_on_startup)
        for name, callback in (("check-updates", self.check), ("view-update", self.show)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=callback: fn())
            app.add_action(action)

    def _check_on_startup(self):
        self.startup_check = None
        if (
            self.config.get("auto_check_updates", True)
            and os.environ.get("FLATPAK_ID") == updates.APP_ID
        ):
            # Every fresh process checks once. The persisted deadline below is
            # only for periodic checks while that process remains alive.
            self.check(manual=False, present=True)
        return False

    def _tick(self):
        if (
            self.config.get("auto_check_updates", True)
            and os.environ.get("FLATPAK_ID") == updates.APP_ID
        ):
            if time.time() >= self.config.get("update_next_check", 0):
                self.check(manual=False)
        return True

    def close(self):
        self.closed = True
        self.cancel.set()
        if self.startup_check is not None:
            GLib.source_remove(self.startup_check)
            self.startup_check = None
        GLib.source_remove(self.timer)

    def _worker(self, work, done):
        self.busy = True
        self.app.hold()

        def run():
            value, error = None, None
            try:
                value = work()
            except Exception as caught:
                error = caught
            GLib.idle_add(finish, value, error)

        def finish(value, error):
            self.busy = False
            try:
                if not self.closed:
                    done(value, error)
            finally:
                self.app.release()
            return False

        threading.Thread(target=run, daemon=True, name="clipper-updates").start()

    def check(self, manual=True, present=False):
        if self._checking:
            self._manual_check_requested |= manual
            self._present_release_requested |= present
            if manual:
                self.app._show_toast(_("Checking for updates…"))
            return
        if self.busy or self.installed:
            if manual:
                self.show()
            return
        if manual:
            if os.environ.get("FLATPAK_ID") != updates.APP_ID:
                self.app._show_toast(
                    _("Install the Flatpak version of Clipper to use in-app updates.")
                )
                return
            self.app._show_toast(_("Checking for updates…"))
        self._checking = True
        self._manual_check_requested = manual
        self._present_release_requested = present
        # Persist before starting, so process handoffs and failures cannot poll
        # GitHub repeatedly. Manual requests still bypass this schedule.
        self.config.set("update_next_check", time.time() + updates.CHECK_INTERVAL)
        etag = self.config.get("update_etag", "")
        cached = self.config.get("update_release_cache", {})

        def work():
            installation = updates.running_installation()
            result = updates.discover(
                updates.current_version(), installation.arch, installation.branch, etag, cached
            )
            return installation, result

        def done(value, error):
            requested = self._manual_check_requested
            present_release = self._present_release_requested
            self._checking = False
            self._manual_check_requested = False
            self._present_release_requested = False
            if error:
                if isinstance(error, urllib.error.HTTPError):
                    retry = error.headers.get("Retry-After", "")
                    reset = error.headers.get("X-RateLimit-Reset", "")
                    until = max(
                        time.time() + updates.CHECK_INTERVAL,
                        time.time() + int(retry) if retry.isdigit() else 0,
                        int(reset) if reset.isdigit() else 0,
                    )
                    self.config.set("update_next_check", until)
                self.app._log(f"Update check failed: {error}")
                if requested:
                    self.app._show_toast(
                        _("Could not check for updates. Check your connection and try again.")
                    )
                return
            self.installation, (self.release, etag, cached) = value
            self.config.set("update_available", asdict(self.release) if self.release else {})
            self.config.set("update_etag", etag)
            self.config.set("update_release_cache", cached)
            self.refresh_banner()
            offer_startup_release = bool(
                self.release
                and self.config.get("update_skipped_version", "") != self.release.version
            )
            if self.release and (
                requested
                or (
                    offer_startup_release
                    and present_release
                    and self.app._is_window_visible()
                )
            ):
                self.show()
            elif not self.release:
                if self.dialog:
                    self.dialog.close()
                if requested:
                    self.app._show_toast(_("Clipper is up to date."))
            if self.dialog:
                self._render()
            if (
                not requested
                and self.release
                and self.config.get("update_skipped_version", "") != self.release.version
            ):
                version = self.release.version
                if (
                    not self.app._is_window_visible()
                    and self.config.get("update_notified_version", "") != version
                ):
                    notification = Gio.Notification.new(_("Clipper update available"))
                    notification.set_body(
                        _("Clipper %(version)s is ready to download.") % {"version": version}
                    )
                    notification.set_default_action("app.view-update")
                    self.app.send_notification("clipper-update", notification)
                    self.config.set("update_notified_version", version)

        self._worker(work, done)

    def refresh_banner(self):
        window = self.app.window
        if not window or not hasattr(window, "update_banner"):
            return
        available = bool(
            self.release
            and (
                self.installed
                or self.config.get("update_skipped_version", "") != self.release.version
            )
        )
        window.update_banner.set_title(
            _("Restart Clipper to finish updating")
            if self.installed
            else _("A Clipper update is available")
        )
        window.update_banner.set_revealed(available)

    def show(self):
        self.app._start_hidden = False
        self.app.do_activate()
        if not self.release and not self.installed:
            self.check()
            return
        parent = self.app.editor_window or self.app.window or self.app.setup_window
        if self.dialog:
            self.dialog.present(parent)
            return
        dialog = Adw.Dialog(title=_("Updates"))
        dialog.set_follows_content_size(True)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            width_request=312,
            margin_top=12,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self.heading = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.heading.add_css_class("title-2")
        box.append(self.heading)
        self.status = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            max_width_chars=36,
            justify=Gtk.Justification.CENTER,
        )
        self.status.add_css_class("dim-label")
        box.append(self.status)

        self.details_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.details_list.add_css_class("boxed-list")

        def add_detail(title):
            row = Adw.ActionRow(title=title)
            value = Gtk.Label(xalign=1, valign=Gtk.Align.CENTER, width_chars=8)
            value.add_css_class("heading")
            value.add_css_class("numeric")
            row.add_suffix(value)
            self.details_list.append(row)
            return value

        self.installed_value = add_detail(_("Installed"))
        self.available_value = add_detail(_("Available"))
        self.available_value.add_css_class("accent")
        self.download_value = add_detail(_("Download"))
        box.append(self.details_list)

        self.progress = Gtk.ProgressBar(visible=False)
        box.append(self.progress)
        self.notes_button = Gtk.Button(label=_("Release notes"), halign=Gtk.Align.CENTER)
        self.notes_button.add_css_class("flat")
        self.notes_button.connect(
            "clicked",
            lambda _b: Gtk.UriLauncher.new(
                f"{updates.REPOSITORY}/releases/tag/v{self.release.version}"
                if self.release
                else f"{updates.REPOSITORY}/releases/latest"
            ).launch(parent, None, None),
        )
        box.append(self.notes_button)
        actions = Gtk.Box(spacing=12, homogeneous=True, margin_top=8)
        self.later_button = Gtk.Button(label=_("Later"))
        self.later_button.connect("clicked", lambda _button: dialog.close())
        actions.append(self.later_button)
        self.button = Gtk.Button(label=_("Update"))
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", self._primary)
        actions.append(self.button)
        box.append(actions)
        self.skip_button = Gtk.Button(label=_("Skip this version"), halign=Gtk.Align.CENTER)
        self.skip_button.add_css_class("flat")
        self.skip_button.connect("clicked", self._skip)
        box.append(self.skip_button)
        toolbar.set_content(box)
        dialog.set_child(toolbar)
        dialog.connect("closed", self._closed)
        self.dialog = dialog
        self._render()
        dialog.present(parent)

    def _closed(self, _dialog):
        self.dialog = None

    def _render(self):
        if not self.dialog:
            return
        if not self.release and not self.installed:
            self.dialog.close()
            return
        self.progress.set_visible(False)
        self.status.set_visible(True)
        self.details_list.set_visible(False)
        self.button.set_sensitive(not self.busy)
        self.button.add_css_class("suggested-action")
        self.notes_button.set_visible(bool(self.release) and not self.installed and not self.busy)
        self.skip_button.set_visible(bool(self.release) and not self.installed and not self.busy)
        self.later_button.set_visible(not self.busy)
        self.later_button.set_label(_("Restart later") if self.installed else _("Later"))
        if self.installed:
            self.heading.set_label(_("Restart Clipper to finish updating"))
            self.status.set_label(_("Update installed. Restart Clipper to use the new version."))
            self.button.set_label(_("Restart now"))
        elif self.release:
            self.heading.set_label(_("Clipper update available"))
            self.status.set_visible(False)
            self.details_list.set_visible(True)
            self.installed_value.set_label(updates.current_version())
            self.available_value.set_label(self.release.version)
            self.download_value.set_label(
                _("%(size)s %(unit)s")
                % {"size": f"{self.release.size / 1024**2:.1f}", "unit": _("MiB")}
            )
            self.button.set_label(_("Update"))

    def _skip(self, _button):
        if self.release and not self.busy and not self.installed:
            self.config.set("update_skipped_version", self.release.version)
            self.app.withdraw_notification("clipper-update")
            self.refresh_banner()
            self.dialog.close()

    def _primary(self, _button):
        if self.busy:
            self.cancel.set()
            self.button.set_sensitive(False)
        elif self.installed:
            self._restart()
        elif self.release:
            self._update()
        else:
            self.check()

    def _update(self):
        release = self.release
        if not release:
            return
        self.cancel.clear()
        self.dialog.set_can_close(False)
        self.notes_button.set_visible(False)
        self.skip_button.set_visible(False)
        self.later_button.set_visible(False)
        self.button.remove_css_class("suggested-action")
        self.details_list.set_visible(False)
        self.status.set_visible(True)
        self.status.set_label(_("Downloading update…"))
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.button.set_label(_("Cancel"))

        def progress(fraction):
            def display():
                if not self.closed:
                    self.progress.set_fraction(fraction)
                return False

            GLib.idle_add(display)

        def work():
            # Resolve again on every installation, including after a tray handoff.
            installation = updates.running_installation()
            if release.ref != f"app/{updates.APP_ID}/{installation.arch}/{installation.branch}":
                raise ValueError("Update does not match this installation")
            cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            bundle = updates.download(release, cache / "clipper/updates", self.cancel, progress)
            return installation, bundle

        def downloaded(value, error):
            if error:
                done(None, error)
                return
            installation, bundle = value
            # Resolve cancellation on the GTK thread before disabling Cancel
            # and starting the irreversible installation phase.
            if self.cancel.is_set():
                bundle.unlink(missing_ok=True)
                done(None, InterruptedError())
                return
            self.installation = installation
            self.status.set_label(_("Installing update…"))
            self.button.set_sensitive(False)
            self._worker(lambda: updates.install(release, installation, bundle), done)

        def done(_value, error):
            self.dialog.set_can_close(True)
            if error:
                self._render()
                if not isinstance(error, InterruptedError):
                    self.app._log(f"Update installation failed: {error}")
                    message = _(
                        "Could not install the update. "
                        "Your current version is still available. Try again."
                    )
                    detail = getattr(error, "detail", "")
                    if detail:
                        message += "\n\n" + _("Details: %(details)s") % {"details": detail}
                    self.status.set_visible(True)
                    self.status.set_label(message)
                return
            self.installed = True
            self.config.set("update_installed_version", release.version)
            self.app.withdraw_notification("clipper-update")
            self._render()
            self.refresh_banner()

        self._worker(work, downloaded)

    def _restart(self):
        if self.busy or getattr(self.app, "_update_restart_pending", False):
            return
        if (
            getattr(self.app, "_editor_open_pending", False)
            or self.app.editor_window is not None
            or getattr(self.app._engine_client, "pending_saves", 0)
        ):
            self.status.set_label(
                _("Close the editor and wait for clip saving to finish before restarting.")
            )
            return
        # Reserve the restart before launching the host helper. No new editor
        # or save may begin after the last safety check.
        self.app._update_restart_pending = True
        if self.dialog:
            self.dialog.set_can_close(False)
        self.button.set_sensitive(False)

        def ready(installation, error):
            if error:
                self.app._update_restart_pending = False
                if self.dialog:
                    self.dialog.set_can_close(True)
                self.button.set_sensitive(True)
                self.app._log(f"Update restart failed: {error}")
                self.status.set_label(
                    _(
                        "Could not restart Clipper. "
                        "Quit and open it again to use the update."
                    )
                )
                return
            self.app._restart_in_background = False
            self.app._restart_in_foreground = False
            self.app.quit()

        def prepare():
            installation = updates.running_installation()
            updates.restart_after_exit(installation)
            return installation

        self._worker(prepare, ready)
