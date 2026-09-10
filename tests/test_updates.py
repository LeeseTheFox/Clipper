import hashlib
import io
import threading
import urllib.error
from dataclasses import replace
from email.message import Message
from pathlib import Path

import pytest
import updates


@pytest.fixture
def release_data():
    name = "Clipper-1.10.0-x86_64.flatpak"
    data = {
        "tag_name": "v1.10.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "size": 4,
                "browser_download_url": f"{updates.REPOSITORY}/releases/download/v1.10.0/{name}",
            }
        ],
    }
    manifest = {
        "schema": 1,
        "version": "1.10.0",
        "asset": name,
        "size": 4,
        "sha256": hashlib.sha256(b"test").hexdigest(),
        "ref": f"app/{updates.APP_ID}/x86_64/master",
        "commit": "a" * 64,
    }
    return data, manifest


def test_numeric_version_comparison(release_data):
    release = updates.parse_release(*release_data, "1.9.9", "x86_64", "master")
    assert release.version == "1.10.0"


@pytest.mark.parametrize("version", ["1.10.0", "2.0.0"])
def test_no_same_version_or_downgrade(release_data, version):
    with pytest.raises(ValueError):
        updates.parse_release(*release_data, version, "x86_64", "master")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", 2),
        ("ref", "app/other.App/x86_64/master"),
        ("size", 5),
        ("sha256", "invalid"),
        ("commit", "invalid"),
        ("version", "1.0.0"),
        ("asset", "wrong.flatpak"),
    ],
)
def test_rejects_mismatched_manifest(release_data, field, value):
    data, manifest = release_data
    manifest[field] = value
    with pytest.raises(ValueError):
        updates.parse_release(data, manifest, "1.0.0", "x86_64", "master")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/file",
        "file:///etc/passwd",
        f"{updates.REPOSITORY}/releases/download/v1.10.0/../file",
    ],
)
def test_rejects_unexpected_asset_url(url):
    with pytest.raises(ValueError):
        updates.asset_url(url, "1.10.0")


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_ignores_unstable_releases(release_data, flag, monkeypatch):
    data, _ = release_data
    data[flag] = True
    monkeypatch.setattr(updates, "read_json", lambda *_args: (data, "etag"))
    assert updates.discover("1.0.0", "x86_64", "master")[0] is None


def test_conditional_request_uses_cached_release(release_data, monkeypatch):
    data, _ = release_data

    def unchanged(url, headers):
        assert headers == {"If-None-Match": "cached-etag"}
        raise urllib.error.HTTPError(url, 304, "unchanged", Message(), None)

    monkeypatch.setattr(updates, "read_json", unchanged)
    assert updates.discover("1.10.0", "x86_64", "master", "cached-etag", data) == (
        None,
        "cached-etag",
        data,
    )


def test_missing_manifest_is_not_reported_as_up_to_date(release_data, monkeypatch):
    monkeypatch.setattr(updates, "read_json", lambda *_args: (release_data[0], ""))
    with pytest.raises(ValueError, match="manifest"):
        updates.discover("1.0.0", "x86_64", "master")


def test_verified_download(release_data, monkeypatch, tmp_path):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"test"))
    progress = []
    target = updates.download(release, tmp_path, threading.Event(), progress.append)
    assert target.read_bytes() == b"test"
    assert progress[-1] == 1
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    "payload,cancelled", [(b"bad!", False), (b"toomuch", False), (b"tes", False), (b"test", True)]
)
def test_bad_or_cancelled_download_never_reaches_installer(
    release_data, monkeypatch, tmp_path, payload, cancelled
):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(payload))
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    with pytest.raises((ValueError, InterruptedError)):
        updates.download(release, tmp_path, cancel, lambda _p: None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("scope", ["--user", "--system", "--installation=custom"])
def test_install_preserves_scope_and_verifies_commit(release_data, monkeypatch, tmp_path, scope):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bundle = tmp_path / "bundle.flatpak"
    bundle.write_bytes(b"test")
    calls = []
    monkeypatch.setattr(updates.subprocess, "run", lambda args, **_kwargs: calls.append(args))
    results = iter(
        [
            "b" * 64,
            "/deployment",
            '<component><releases><release version="1.0.0"/></releases></component>',
            release.commit,
        ]
    )
    monkeypatch.setattr(updates, "host_output", lambda *_args: next(results))
    updates.install(release, updates.Installation(scope, "x86_64", "master"), bundle)
    prefix = ["flatpak-spawn", "--host", "--directory=/"]
    if scope != "--user":
        prefix.append("pkexec")
    assert calls[0] == [
        *prefix, "flatpak", scope, "install", "--assumeyes", "--or-update",
        "--bundle", str(Path.home() / ".var/app" / updates.APP_ID / "cache/bundle.flatpak"),
    ]
    assert "--bundle" in calls[0]
    assert calls[0][-1] == str(Path.home() / ".var/app" / updates.APP_ID / "cache/bundle.flatpak")
    assert not bundle.exists()


def test_host_cache_path_uses_flatpak_instance_path(monkeypatch, tmp_path):
    cache = tmp_path / "sandbox-cache"
    bundle = cache / "clipper/updates/update.flatpak"

    def read(info, _path):
        info.read_dict({
            "Application": {"name": updates.APP_ID},
            "Instance": {"instance-path": "/host/custom/app-data"},
        })
        return ["/.flatpak-info"]

    monkeypatch.setattr(updates.configparser.ConfigParser, "read", read)
    assert updates.host_cache_path(bundle, cache) == Path(
        "/host/custom/app-data/cache/clipper/updates/update.flatpak"
    )


def test_host_cache_path_ignores_non_application_flatpak_info(monkeypatch, tmp_path):
    cache = tmp_path / "sandbox-cache"
    bundle = cache / "clipper/updates/update.flatpak"

    def read(info, _path):
        info.read_dict({"Runtime": {"runtime": "org.gnome.Sdk/x86_64/50"}})
        return ["/.flatpak-info"]

    monkeypatch.setattr(updates.configparser.ConfigParser, "read", read)
    assert updates.host_cache_path(bundle, cache) == Path.home() / (
        f".var/app/{updates.APP_ID}/cache/clipper/updates/update.flatpak"
    )


def test_install_accepts_commit_deployed_despite_flatpak_error(
    release_data, monkeypatch, tmp_path
):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bundle = tmp_path / "bundle.flatpak"
    bundle.write_bytes(b"test")
    results = iter(
        [
            "b" * 64,
            "/deployment",
            '<component><releases><release version="1.0.0"/></releases></component>',
            release.commit,
            release.commit,
        ]
    )
    monkeypatch.setattr(updates, "host_output", lambda *_args: next(results))

    def fail(*_args, **_kwargs):
        raise updates.subprocess.CalledProcessError(1, [], stderr="already installed")

    monkeypatch.setattr(updates.subprocess, "run", fail)
    updates.install(release, updates.Installation("--user", "x86_64", "master"), bundle)
    assert not bundle.exists()


@pytest.mark.parametrize("scope", ["--user", "--system", "--installation=custom"])
@pytest.mark.parametrize("exit_code", [1, 126, 127])
def test_install_preserves_flatpak_failure_detail(
    release_data, monkeypatch, tmp_path, scope, exit_code
):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bundle = tmp_path / "bundle.flatpak"
    bundle.write_bytes(b"test")
    results = iter(
        [
            "b" * 64,
            "/deployment",
            '<component><releases><release version="1.0.0"/></releases></component>',
            "b" * 64,
        ]
    )
    monkeypatch.setattr(updates, "host_output", lambda *_args: next(results))

    calls = []

    def fail(*args, **_kwargs):
        calls.append(args)
        raise updates.subprocess.CalledProcessError(
            exit_code, [], stderr="Installing…\nerror: Not authorized"
        )

    monkeypatch.setattr(updates.subprocess, "run", fail)
    with pytest.raises(updates.InstallError) as caught:
        updates.install(release, updates.Installation(scope, "x86_64", "master"), bundle)
    assert caught.value.detail == "error: Not authorized"
    assert bundle.exists()
    assert len(calls) == 1  # No automatic retry or fallback to another installation.


def test_install_does_not_claim_success_for_wrong_commit(release_data, monkeypatch, tmp_path):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bundle = tmp_path / "bundle.flatpak"
    bundle.write_bytes(b"test")
    monkeypatch.setattr(updates.subprocess, "run", lambda *_args, **_kwargs: None)
    results = iter(
        [
            "b" * 64,
            "/deployment",
            '<component><releases><release version="1.0.0"/></releases></component>',
            "b" * 64,
        ]
    )
    monkeypatch.setattr(updates, "host_output", lambda *_args: next(results))
    with pytest.raises(ValueError, match="commit"):
        updates.install(
            replace(release, commit="c" * 64),
            updates.Installation("--user", "x86_64", "master"),
            bundle,
        )


def test_already_installed_commit_finishes_without_reinstall(release_data, monkeypatch, tmp_path):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    bundle = tmp_path / "bundle.flatpak"
    bundle.write_bytes(b"test")
    monkeypatch.setattr(updates, "host_output", lambda *_args: release.commit)
    calls = []
    monkeypatch.setattr(updates.subprocess, "run", lambda *args, **_kwargs: calls.append(args))
    updates.install(release, updates.Installation("--user", "x86_64", "master"), bundle)
    assert not calls
    assert not bundle.exists()


def test_concurrent_newer_installation_is_not_downgraded(release_data, monkeypatch, tmp_path):
    release = updates.parse_release(*release_data, "1.0.0", "x86_64", "master")
    results = iter(
        [
            "b" * 64,
            "/deployment",
            '<component><releases><release version="2.0.0"/></releases></component>',
        ]
    )
    monkeypatch.setattr(updates, "host_output", lambda *_args: next(results))
    with pytest.raises(ValueError, match="newer version"):
        updates.install(
            release, updates.Installation("--user", "x86_64", "master"), tmp_path / "bundle.flatpak"
        )


@pytest.mark.parametrize(
    "name,scope", [("user", "--user"), ("system", "--system"), ("custom", "--installation=custom")]
)
def test_discovery_preserves_running_installation_after_deployment_changes(
    monkeypatch, name, scope
):
    root = f"/installation/app/{updates.APP_ID}/x86_64/master"

    def read(info, _path):
        info.read_dict({
            "Application": {"name": updates.APP_ID},
            "Instance": {"arch": "x86_64", "branch": "master", "app-path": f"{root}/old/files"},
        })
        return ["/.flatpak-info"]

    monkeypatch.setattr(updates.configparser.ConfigParser, "read", read)

    def output(*args):
        if args[1] == "list":
            return f"{updates.APP_ID}\tx86_64\tmaster\t{name}"
        assert args == (
            "flatpak", scope, "info", "--arch=x86_64", "--show-location", updates.APP_ID, "master"
        )
        return f"{root}/new"

    monkeypatch.setattr(updates, "host_output", output)
    assert updates.running_installation() == updates.Installation(scope, "x86_64", "master")
