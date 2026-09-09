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
    confirmations = []
    confirm = SimpleNamespace(
        add_response=lambda *_args: None,
        set_close_response=lambda *_args: None,
        choose=lambda *_args: confirmations.append(_args[-1]),
        choose_finish=lambda result: result,
    )
    monkeypatch.setattr(update_controller.Adw.AlertDialog, "new", lambda *_args: confirm)
    pending = []
    updater._worker = lambda work, done: pending.append(done)
    quit_calls = []
    updater.app.quit = lambda: quit_calls.append(True)
    updater._restart()
    updater._restart()
    assert len(confirmations) == 1
    confirmations[0](confirm, "restart")
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
