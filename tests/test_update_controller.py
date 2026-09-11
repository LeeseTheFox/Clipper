from dataclasses import asdict
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import update_controller
import updates


class Config(dict):
    def set(self, key, value):
        self[key] = value


class Widget:
    def set_label(self, value):
        self.label = value

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

    def set_can_close(self, value):
        self.can_close = value

    def set_fraction(self, value):
        self.fraction = value

    def remove_css_class(self, _value):
        pass


def controller(monkeypatch, config=None):
    config = Config() if config is None else config
    app = SimpleNamespace(
        _config=config,
        add_action=lambda _a: None,
        _log=lambda _m: None,
        toasts=[],
        _is_window_visible=lambda: False,
        window=None,
        send_notification=lambda *_args: None,
    )
    app._show_toast = app.toasts.append
    monkeypatch.setattr(update_controller.GLib, "timeout_add_seconds", lambda *_args: 1)
    monkeypatch.setattr(update_controller.GLib, "idle_add", lambda *_args: 2)
    monkeypatch.setenv("FLATPAK_ID", updates.APP_ID)
    monkeypatch.setattr(updates, "current_version", lambda: "1.0.0")
    result = update_controller.UpdateController(app)

    def worker(work, done):
        try:
            value = work()
        except Exception as error:
            done(None, error)
        else:
            done(value, None)

    result._worker = worker
    result.show = lambda: None
    result.status = Widget()
    result.button = Widget()
    return result


def available_release():
    return updates.Release(
        "1.1.0",
        f"{updates.REPOSITORY}/releases/download/v1.1.0/test.flatpak",
        4,
        "a" * 64,
        f"app/{updates.APP_ID}/x86_64/master",
        "b" * 64,
        "",
    )


@pytest.mark.parametrize("detail", [
    "Failed to install bundle io.github.leesethefox.Clipper: "
    "Flatpak system operation InstallBundle not allowed for user",
    "/very/long/path/" + "a" * 1000,
])
def test_update_error_does_not_widen_dialog(monkeypatch, tmp_path, detail):
    from gi.repository import Gdk

    if Gdk.Display.get_default() is None:
        pytest.skip("GTK display required for layout measurements")
    updater = controller(monkeypatch)
    updater.release = available_release()
    updater.app.do_activate = lambda: None
    updater.app.editor_window = None
    updater.app.setup_window = None
    updater.app.withdraw_notification = lambda *_args: None
    monkeypatch.setattr(update_controller.Adw.Dialog, "present", lambda *_args: None)
    update_controller.UpdateController.show(updater)
    content = updater.dialog.get_child()
    orientation = update_controller.Gtk.Orientation.HORIZONTAL
    before = content.measure(orientation, -1).natural
    monkeypatch.setattr(
        updates, "running_installation",
        lambda: updates.Installation("--system", "x86_64", "master"),
    )
    monkeypatch.setattr(updates, "download", lambda *_args: tmp_path / "update.flatpak")

    def fail(*_args):
        raise updates.InstallError(detail)

    monkeypatch.setattr(updates, "install", fail)
    updater._update()
    after = content.measure(orientation, -1).natural
    assert after <= before
    assert detail in updater.status.get_label()
    assert not updater.installed
    assert "update_installed_version" not in updater.config
    assert updater.dialog.get_can_close()
    assert updater.button.get_sensitive()
    assert updater.button.get_label() == "Update"

    # A rejected/cancelled authorization leaves the dialog usable for retry.
    monkeypatch.setattr(updates, "install", lambda *_args: None)
    updater._update()
    assert updater.installed
    assert updater.config["update_installed_version"] == updater.release.version


def test_daily_schedule_survives_process_handoff(monkeypatch):
    first = controller(monkeypatch)
    monkeypatch.setattr(
        updates, "running_installation", lambda: updates.Installation("--user", "x86_64", "master")
    )
    calls = []
    monkeypatch.setattr(updates, "discover", lambda *_args: (calls.append(True), "", {}))
    monkeypatch.setattr(update_controller.os.path, "exists", lambda _path: True)
    first._tick()
    second = controller(monkeypatch, first.config)
    second._tick()
    assert len(calls) == 1


def test_automatic_check_runs_on_every_startup_despite_daily_deadline(monkeypatch):
    checks = []
    first = controller(monkeypatch, Config(update_next_check=float("inf")))
    first.check = lambda **kwargs: checks.append(kwargs)
    first._check_on_startup()
    second = controller(monkeypatch, first.config)
    second.check = lambda **kwargs: checks.append(kwargs)
    second._check_on_startup()
    assert checks == [
        {"manual": False, "present": True},
        {"manual": False, "present": True},
    ]


def test_preference_disables_automatic_startup_check(monkeypatch):
    updater = controller(monkeypatch, Config(auto_check_updates=False))
    checks = []
    updater.check = lambda **kwargs: checks.append(kwargs)
    updater._check_on_startup()
    assert checks == []


def test_preference_disables_automatic_checks_but_manual_still_works(monkeypatch):
    updater = controller(monkeypatch, Config(auto_check_updates=False))
    calls = []
    updater.check = lambda **_kwargs: calls.append(True)
    updater._tick()
    assert calls == []
    updater.check()
    assert calls == [True]


def test_failure_is_not_reported_as_up_to_date_and_can_retry(monkeypatch):
    updater = controller(monkeypatch)

    def unavailable():
        raise OSError("offline")

    monkeypatch.setattr(updates, "running_installation", unavailable)
    updater.check()
    assert "Could not check" in updater.app.toasts[-1]
    assert not updater._checking
    assert updater.dialog is None
    assert updater.config["update_next_check"] > update_controller.time.time()


def test_manual_check_waits_for_result_and_uses_toast_when_current(monkeypatch):
    updater = controller(monkeypatch)
    pending = []
    shown = []
    updater._worker = lambda work, done: pending.append(done)
    updater.show = lambda: shown.append(True)
    updater.check()
    assert not shown
    assert updater.app.toasts == ["Checking for updates…"]
    pending[0]((updates.Installation("--user", "x86_64", "master"), (None, "", {})), None)
    assert updater.app.toasts[-1] == "Clipper is up to date."
    assert not shown
    assert updater.dialog is None


def test_manual_check_offers_update_only_after_new_version_found(monkeypatch):
    updater = controller(monkeypatch)
    pending = []
    shown = []
    updater._worker = lambda work, done: pending.append(done)
    updater.show = lambda: shown.append(updater.release)
    updater.check()
    assert not shown
    release = available_release()
    pending[0]((updates.Installation("--user", "x86_64", "master"), (release, "", {})), None)
    assert shown == [release]
    assert "Clipper is up to date." not in updater.app.toasts


def test_visible_startup_check_opens_available_update(monkeypatch):
    updater = controller(monkeypatch)
    updater.app._is_window_visible = lambda: True
    shown = []
    updater.show = lambda: shown.append(updater.release)
    monkeypatch.setattr(
        updates, "running_installation", lambda: updates.Installation("--user", "x86_64", "master")
    )
    release = available_release()
    monkeypatch.setattr(updates, "discover", lambda *_args: (release, "", {}))
    updater.check(manual=False, present=True)
    assert shown == [release]


def test_visible_startup_check_does_not_open_skipped_update(monkeypatch):
    release = available_release()
    updater = controller(monkeypatch, Config(update_skipped_version=release.version))
    updater.app._is_window_visible = lambda: True
    shown = []
    updater.show = lambda: shown.append(updater.release)
    monkeypatch.setattr(
        updates, "running_installation", lambda: updates.Installation("--user", "x86_64", "master")
    )
    monkeypatch.setattr(updates, "discover", lambda *_args: (release, "", {}))
    updater.check(manual=False, present=True)
    assert shown == []


def test_manual_check_joins_background_check_without_opening_dialog(monkeypatch):
    updater = controller(monkeypatch)
    pending = []
    shown = []
    updater._worker = lambda work, done: pending.append(done)
    updater.show = lambda: shown.append(True)
    updater.check(manual=False)
    updater.check()
    assert len(pending) == 1
    assert not shown
    pending[0]((updates.Installation("--user", "x86_64", "master"), (None, "", {})), None)
    assert updater.app.toasts[-1] == "Clipper is up to date."


def test_stale_notification_checks_again_without_empty_dialog(monkeypatch):
    updater = controller(monkeypatch)
    updater.app.do_activate = lambda: None
    checks = []
    updater.check = lambda: checks.append(True)
    update_controller.UpdateController.show(updater)
    assert checks == [True]
    assert updater.dialog is None


def test_cached_update_and_restart_state_survive_handoff(monkeypatch):
    release = available_release()
    updater = controller(
        monkeypatch,
        Config(update_available=asdict(release), update_installed_version=release.version),
    )
    assert updater.release == release
    assert updater.installed


def test_notification_is_sent_once_per_version(monkeypatch):
    updater = controller(monkeypatch)
    release = available_release()
    monkeypatch.setattr(
        updates, "running_installation", lambda: updates.Installation("--user", "x86_64", "master")
    )
    monkeypatch.setattr(updates, "discover", lambda *_args: (release, "", {}))
    sent = []
    updater.app.send_notification = lambda *args: sent.append(args)
    updater.check(manual=False)
    updater.check(manual=False)
    assert len(sent) == 1
    assert updater.config["update_notified_version"] == release.version


def test_skipped_version_suppresses_notification(monkeypatch):
    release = available_release()
    updater = controller(monkeypatch, Config(update_skipped_version=release.version))
    monkeypatch.setattr(
        updates, "running_installation", lambda: updates.Installation("--user", "x86_64", "master")
    )
    monkeypatch.setattr(updates, "discover", lambda *_args: (release, "", {}))
    sent = []
    updater.app.send_notification = lambda *args: sent.append(args)
    updater.check(manual=False)
    assert updater.release == release
    assert not sent


def test_restart_is_deferred_while_saving(monkeypatch):
    updater = controller(monkeypatch)
    updater.app.editor_window = None
    updater.app._engine_client = SimpleNamespace(pending_saves=1)
    updater._restart()
    assert "wait for clip saving" in updater.status.label


def test_restart_is_deferred_while_editor_is_being_loaded(monkeypatch):
    updater = controller(monkeypatch)
    updater.app.editor_window = None
    updater.app._editor_open_pending = True
    updater.app._engine_client = SimpleNamespace(pending_saves=0)
    updater._restart()
    assert "Close the editor" in updater.status.label


@pytest.mark.parametrize("failed", [False, True])
def test_restart_reserves_app_until_helper_is_ready(monkeypatch, failed):
    updater = controller(monkeypatch)
    updater.app.editor_window = None
    updater.app.setup_window = None
    updater.app._engine_client = SimpleNamespace(pending_saves=0)
    updater.dialog = Widget()
    pending = []
    updater._worker = lambda work, done: pending.append(done)
    quit_calls = []
    updater.app.quit = lambda: quit_calls.append(True)
    updater._restart()
    updater._restart()
    assert len(pending) == 1
    assert updater.app._update_restart_pending
    assert not updater.dialog.can_close
    assert not updater.button.sensitive
    assert not quit_calls
    pending[0](None, OSError("host unavailable") if failed else None)
    if failed:
        assert not updater.app._update_restart_pending
        assert updater.dialog.can_close
        assert updater.button.sensitive
        assert not quit_calls
    else:
        assert quit_calls == [True]


@pytest.mark.parametrize("app_id", ["", "dev.zed.Zed"])
def test_native_run_does_not_update_another_flatpak(monkeypatch, app_id):
    updater = controller(monkeypatch)
    monkeypatch.setenv("FLATPAK_ID", app_id)
    workers = []
    updater._worker = lambda *args: workers.append(args)
    updater._tick()
    updater.check()
    assert not workers
    assert "Install the Flatpak version" in updater.app.toasts[-1]


@pytest.mark.parametrize("cancelled", [False, True])
def test_cancel_at_download_completion_prevents_install(monkeypatch, tmp_path, cancelled):
    updater = controller(monkeypatch)
    updater.release = available_release()
    updater.dialog = Widget()
    updater.notes_button = Widget()
    updater.skip_button = Widget()
    updater.later_button = Widget()
    updater.progress = Widget()
    updater.details_list = Widget()
    updater._render = lambda: None
    pending = []
    updater._worker = lambda work, done: pending.append((work, done))
    installation = updates.Installation("--user", "x86_64", "master")
    monkeypatch.setattr(updates, "running_installation", lambda: installation)
    bundle = tmp_path / "update.flatpak"
    bundle.write_bytes(b"test")
    monkeypatch.setattr(updates, "download", lambda *_args: bundle)
    installed = []

    def install(*args):
        assert updater.button.sensitive is False
        installed.append(args)

    monkeypatch.setattr(updates, "install", install)
    updater._update()
    work, done = pending.pop()
    value = work()
    if cancelled:
        updater.cancel.set()
    done(value, None)
    if cancelled:
        assert not pending
        assert not bundle.exists()
        assert updater.dialog.can_close
    else:
        assert updater.status.label == "Installing update…"
        pending.pop()[0]()
        assert installed == [(updater.release, installation, bundle)]
