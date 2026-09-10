"""GitHub release discovery and verified Flatpak bundle installation.

No GTK calls here: callers run blocking operations outside the main loop.
Release metadata is trusted through HTTPS to the project's GitHub repository.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

APP_ID = "io.github.leesethefox.Clipper"
REPOSITORY = "https://github.com/LeeseTheFox/Clipper"
LATEST_API = "https://api.github.com/repos/LeeseTheFox/Clipper/releases/latest"
CHECK_INTERVAL = 24 * 60 * 60
MAX_BUNDLE_SIZE = 4 * 1024**3


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Invalid release version")
    return tuple(map(int, value.split(".")))


def current_version() -> str:
    path = Path(f"/app/share/metainfo/{APP_ID}.metainfo.xml")
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / "packaging/flatpak" / path.name
    release = ET.parse(path).find("./releases/release")
    if release is None:
        raise ValueError("Missing application version")
    version = release.attrib["version"]
    version_tuple(version)
    return version


def asset_url(url: str, version: str) -> str:
    prefix = f"{REPOSITORY}/releases/download/v{version}/"
    if not url.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_.-]+", url[len(prefix) :]):
        raise ValueError("Unexpected release asset URL")
    return url


def read_json(url: str, headers: dict | None = None) -> tuple[dict, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Clipper-updater",
            "Accept": "application/vnd.github+json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Release metadata is too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid release metadata")
        return result, response.headers.get("ETag", "")


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: str
    ref: str
    commit: str
    notes: str


def parse_release(data: dict, manifest: dict, installed: str, arch: str, branch: str) -> Release:
    version = str(data.get("tag_name", "")).removeprefix("v")
    if (
        data.get("draft")
        or data.get("prerelease")
        or version_tuple(version) <= version_tuple(installed)
    ):
        raise ValueError("Release is not a newer stable version")
    ref = f"app/{APP_ID}/{arch}/{branch}"
    name = f"Clipper-{version}-{arch}.flatpak"
    asset = next(a for a in data["assets"] if a["name"] == name and a["state"] == "uploaded")
    if (
        manifest.get("schema") != 1
        or manifest.get("version") != version
        or manifest.get("ref") != ref
        or manifest.get("asset") != name
        or manifest.get("size") != asset["size"]
    ):
        raise ValueError("Release manifest does not match this installation")
    size = manifest["size"]
    if type(size) is not int or not 0 < size <= MAX_BUNDLE_SIZE:
        raise ValueError("Invalid bundle size")
    for key in ("sha256", "commit"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(key, ""))):
            raise ValueError("Invalid release checksum or commit")
    return Release(
        version,
        asset_url(asset["browser_download_url"], version),
        size,
        manifest["sha256"],
        ref,
        manifest["commit"],
        str(data.get("body") or "")[:30000],
    )


def discover(
    installed: str, arch: str, branch: str, etag: str = "", cached: dict | None = None
) -> tuple[Release | None, str, dict]:
    try:
        data, new_etag = read_json(LATEST_API, {"If-None-Match": etag} if etag and cached else {})
    except urllib.error.HTTPError as error:
        if error.code == 304 and cached:
            data, new_etag = cached, etag
        elif error.code == 404:
            return None, "", {}
        else:
            raise
    version = str(data.get("tag_name", "")).removeprefix("v")
    if (
        data.get("draft")
        or data.get("prerelease")
        or version_tuple(version) <= version_tuple(installed)
    ):
        return None, new_etag, data
    manifests = [a for a in data.get("assets", []) if a.get("name") == f"update-{arch}.json"]
    if not manifests:
        raise ValueError("This release has no compatible update manifest")
    manifest, _ = read_json(asset_url(manifests[0]["browser_download_url"], version))
    return parse_release(data, manifest, installed, arch, branch), new_etag, data


def host_command(*args: str) -> list[str]:
    return ["flatpak-spawn", "--host", "--directory=/", *args]


def host_output(*args: str) -> str:
    return subprocess.run(
        host_command(*args), check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@dataclass(frozen=True)
class Installation:
    scope: str
    arch: str
    branch: str

    def command(self, operation: str, *args: str) -> list[str]:
        return host_command("flatpak", self.scope, operation, *args)


class InstallError(RuntimeError):
    """A Flatpak transaction failure with a safe detail for the update dialog."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"Flatpak installation failed: {detail}")


def running_installation() -> Installation:
    info = configparser.ConfigParser(interpolation=None)
    if not info.read("/.flatpak-info") or info["Application"]["name"] != APP_ID:
        raise ValueError("Updates require an installed Clipper Flatpak")
    instance = info["Instance"]
    arch, branch = instance["arch"], instance["branch"]
    # Compare deployment parents, since another updater may already have changed
    # the active commit since this sandbox started.
    running_root = Path(instance["app-path"]).parent.parent
    rows = host_output("flatpak", "list", "--app", "--columns=application,arch,branch,installation")
    for row in rows.splitlines():
        fields = row.split("\t")
        if len(fields) != 4 or fields[:3] != [APP_ID, arch, branch]:
            continue
        name = fields[3]
        scope = {"user": "--user", "system": "--system"}.get(name, f"--installation={name}")
        location = host_output(
            "flatpak", scope, "info", f"--arch={arch}", "--show-location", APP_ID, branch
        )
        if Path(location).parent == running_root:
            return Installation(scope, arch, branch)
    raise ValueError("Could not identify the running Flatpak installation")


def download(
    release: Release, destination: Path, cancel: threading.Event, progress: Callable[[float], None]
) -> Path:
    asset_url(release.url, release.version)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"Clipper-{release.version}.flatpak"
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(release.url, headers={"User-Agent": "Clipper-updater"})
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                if cancel.is_set():
                    raise InterruptedError("Download cancelled")
                total += len(chunk)
                if total > release.size:
                    raise ValueError("Download exceeds the published size")
                output.write(chunk)
                digest.update(chunk)
                progress(total / release.size)
        if cancel.is_set():
            raise InterruptedError("Download cancelled")
        if total != release.size or digest.hexdigest() != release.sha256:
            raise ValueError("Downloaded bundle failed verification")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def host_cache_path(bundle: Path, cache: Path) -> Path:
    """Translate an app-cache path in the sandbox to its host path."""
    relative = bundle.relative_to(cache)
    info = configparser.ConfigParser(interpolation=None)
    if info.read("/.flatpak-info") and info["Application"].get("name") == APP_ID:
        instance_path = info["Instance"].get("instance-path", "")
        if instance_path:
            return Path(instance_path) / "cache" / relative
    # Keep native tests and development helpers usable outside a sandbox.
    return Path.home() / ".var/app" / APP_ID / "cache" / relative


def install(release: Release, installation: Installation, bundle: Path) -> None:
    existing_commit = host_output(
        "flatpak", installation.scope, "info", "--show-commit", release.ref
    )
    if existing_commit == release.commit:
        bundle.unlink(missing_ok=True)
        return
    location = host_output("flatpak", installation.scope, "info", "--show-location", release.ref)
    metadata = host_output(
        "cat", str(Path(location) / f"files/share/metainfo/{APP_ID}.metainfo.xml")
    )
    deployed_release = ET.fromstring(metadata).find("./releases/release")
    if deployed_release is None:
        raise ValueError("Could not verify the deployed version")
    if version_tuple(deployed_release.attrib["version"]) > version_tuple(release.version):
        raise ValueError("A newer version is already installed; restart Clipper")
    # Resolve the host-visible equivalent of the sandbox's private cache. The
    # authoritative instance path also covers nonstandard host home locations.
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    host_path = host_cache_path(bundle, cache)
    try:
        subprocess.run(
            installation.command(
                "install", "--assumeyes", "--or-update", "--bundle", str(host_path)
            ),
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.CalledProcessError as error:
        # A concurrent transaction can deploy the requested commit while this
        # Flatpak process still exits unsuccessfully. Verify before reporting a
        # failure so the UI never calls a completed update unsuccessful.
        try:
            commit = host_output(
                "flatpak", installation.scope, "info", "--show-commit", release.ref
            )
        except (OSError, subprocess.SubprocessError):
            commit = ""
        if commit != release.commit:
            lines = [line.strip() for line in error.stderr.splitlines() if line.strip()]
            detail = lines[-1] if lines else "Flatpak exited without an error message"
            raise InstallError(detail[-1000:]) from error
    commit = host_output("flatpak", installation.scope, "info", "--show-commit", release.ref)
    if commit != release.commit:
        raise ValueError("Installed commit does not match the release")
    bundle.unlink(missing_ok=True)


def restart_after_exit(installation: Installation) -> None:
    """Launch the new deployment once this Flatpak instance has exited."""
    info = configparser.ConfigParser(interpolation=None)
    info.read("/.flatpak-info")
    instance = info["Instance"]["instance-id"]
    # All values are positional arguments, never interpolated into shell code.
    script = (
        "(instance=$1; shift; "
        "for attempt in $(seq 1 120); do "
        'if ! flatpak ps --columns=instance | grep -Fxq "$instance"; then '
        'exec "$@"; fi; sleep 0.5; done; exit 1) </dev/null >/dev/null 2>&1 &'
    )
    subprocess.run(
        host_command(
            "sh",
            "-c",
            script,
            "clipper-update-restart",
            instance,
            "flatpak",
            installation.scope,
            "run",
            f"--arch={installation.arch}",
            f"--branch={installation.branch}",
            APP_ID,
        ),
        check=True,
        timeout=15,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
