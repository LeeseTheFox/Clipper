#!/usr/bin/env python3
"""Render the README gallery with the pinned GTK runtime and bundled scene."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tools/screenshot_fixture"
DEFAULT_OUTPUT = REPO / "assets/screenshots"
SCENES = ("games", "editor", "audio", "clips")
THEMES = ("light", "dark")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_environment(lock: dict) -> dict[str, str]:
    """Reuse a cached SDK commit, or provision a separate screenshot installation."""
    environment = os.environ.copy()
    probe = ["flatpak", "run", "--arch=x86_64", f"--commit={lock['commit']}",
             "--command=true", lock["runtime"]]
    result = subprocess.run(probe, capture_output=True, env=environment)
    if result.returncode == 0:
        return environment
    cache = Path(environment.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    environment["FLATPAK_USER_DIR"] = str(cache / "clipper-screenshots/flatpak")
    result = subprocess.run(probe, capture_output=True, env=environment)
    if result.returncode == 0:
        return environment
    print("Preparing the pinned screenshot SDK in a separate Flatpak installation...", flush=True)
    commands = [
        ["flatpak", "--user", "remote-add", "--if-not-exists", "flathub",
         "https://flathub.org/repo/flathub.flatpakrepo"],
        ["flatpak", "--user", "install", "--assumeyes", "--noninteractive", "flathub",
         lock["runtime"]],
        ["flatpak", "--user", "update", "--assumeyes", "--noninteractive",
         f"--commit={lock['commit']}", lock["runtime"]],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        if result.returncode:
            details = result.stderr + result.stdout
            raise RuntimeError(f"Cannot provision the pinned SDK:\n{details}")
    return environment


def manifest_source(module: str) -> tuple[str, str]:
    manifest = (REPO / "packaging/flatpak/io.github.leesethefox.Clipper.yml").read_text()
    block = re.search(rf"^  - name: {re.escape(module)}\n(.*?)(?=^  - name: |\Z)",
                      manifest, re.MULTILINE | re.DOTALL)
    if block is None:
        raise RuntimeError(f"Missing manifest module: {module}")
    url = re.search(r"^        url: (\S+)$", block[1], re.MULTILINE)
    checksum = re.search(r"^        sha256: ([0-9a-f]{64})$", block[1], re.MULTILINE)
    if url is None or checksum is None:
        raise RuntimeError(f"Missing pinned download in manifest module: {module}")
    return url[1], checksum[1]


def prepare_sources(destination: Path) -> None:
    """Reuse the manifest pins; never put dependency assets in the repository."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache = cache / "clipper-screenshots/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for module in ("icon-development-kit", "xapp-trash-icon", "python3-vdf"):
        url, checksum = manifest_source(module)
        archive = cache / checksum
        if not archive.is_file() or digest(archive) != checksum:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read()
            if hashlib.sha256(payload).hexdigest() != checksum:
                raise RuntimeError(f"Checksum mismatch: {module}")
            archive.write_bytes(payload)
        if module == "python3-vdf":
            shutil.copyfile(archive, destination / "vdf.whl")
            continue
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts or ".." in parts or not member.isfile():
                    continue
                relative = Path(*parts)
                if module == "xapp-trash-icon":
                    if relative != Path("icons/symbolic/xsi-user-trash-symbolic.svg"):
                        continue
                    target = destination / "icons" / relative.name
                else:
                    if relative.parts[0] != "data":
                        continue
                    target = destination / "icon-library" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = source.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"Cannot read {member.name}")
                with stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
    appearance = json.loads((FIXTURE / "environment.json").read_text())["appearance"]
    fonts = destination / "fonts"
    fonts.mkdir()
    for name, source in appearance["fonts"].items():
        checksum = source["sha256"]
        cached = cache / checksum
        if not cached.is_file() or digest(cached) != checksum:
            with urllib.request.urlopen(source["url"], timeout=60) as response:
                payload = response.read()
            if hashlib.sha256(payload).hexdigest() != checksum:
                raise RuntimeError(f"Checksum mismatch: {name}")
            cached.write_bytes(payload)
        shutil.copyfile(cached, fonts / name)


def readme_with_themes(content: str, language: str) -> str:
    """Keep each README's translated alt text and make its gallery theme-aware."""
    for scene in SCENES:
        image = re.compile(
            rf'(?P<indent> *)<img src="assets/screenshots/{re.escape(language)}/'
            rf'{scene}-dark\.png"(?P<attrs>[^>]+)>'
        )
        matches = list(image.finditer(content))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one {scene} screenshot in README for {language}")
        match = matches[0]
        # Replacing the whole existing picture makes repeated runs idempotent.
        start, end = match.span()
        picture_start = content.rfind("  <picture>\n", 0, start)
        if picture_start >= 0 and "</picture>" not in content[picture_start:start]:
            start = picture_start
            closing = content.find("</picture>", end)
            if closing < 0:
                raise RuntimeError(f"Unclosed screenshot picture for {language}/{scene}")
            end = closing + len("</picture>")
        prefix = f"assets/screenshots/{language}/{scene}"
        picture = (
            '  <picture>\n'
            f'    <source media="(prefers-color-scheme: light)" srcset="{prefix}-light.png">\n'
            f'    <source media="(prefers-color-scheme: dark)" srcset="{prefix}-dark.png">\n'
            f'    <img src="{prefix}-dark.png"{match["attrs"]}>\n'
            '  </picture>'
        )
        content = content[:start] + picture + content[end:]
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true",
                        help="regenerate and compare without replacing screenshots or READMEs")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    lock = json.loads((FIXTURE / "environment.json").read_text())
    for name, checksum in lock["assets"].items():
        if digest(FIXTURE / name) != checksum:
            raise RuntimeError(f"Screenshot fixture changed without updating its checksum: {name}")
    if shutil.which("flatpak") is None:
        raise RuntimeError("Install Flatpak before running this generator")
    runtime = lock["runtime"]
    environment = runtime_environment(lock)
    languages = sorted(path.stem for path in (REPO / "po").glob("*.po"))
    readmes = {}
    if output == DEFAULT_OUTPUT:
        for language in languages:
            path = REPO / ("README.md" if language == "en" else f"README.{language}.md")
            readmes[path] = readme_with_themes(path.read_text(), language)
    with tempfile.TemporaryDirectory(prefix="clipper-screenshots-") as temporary:
        work = Path(temporary)
        prepare_sources(work)
        command = [
            "flatpak", "run", "--arch=x86_64", f"--commit={lock['commit']}",
            "--sandbox", "--unshare=network", "--nodevice=all", "--no-a11y-bus",
            "--no-session-bus", "--no-documents-portal", "--clear-env",
            f"--filesystem={REPO}:ro", f"--filesystem={work}",
            "--env=LANG=C.UTF-8", "--env=LC_ALL=C.UTF-8", "--env=TZ=UTC",
            "--env=GDK_BACKEND=broadway", "--env=BROADWAY_DISPLAY=:1",
            "--env=GSK_RENDERER=cairo", "--env=GDK_SCALE=1", "--env=GDK_DPI_SCALE=1",
            "--env=GTK_A11Y=none", "--env=GSETTINGS_BACKEND=memory",
            "--command=dbus-run-session", runtime, "--", "python3",
            str(REPO / "tools/render_screenshots.py"), str(work), *languages,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                                env=environment)
        if result.returncode:
            raise RuntimeError(result.stderr + result.stdout)
        print(result.stdout.strip())
        paths = [Path(language) / f"{scene}-{theme}.png"
                 for language in languages for theme in THEMES for scene in SCENES]
        changed = []
        for relative in paths:
            generated = work / "output" / relative
            target = output / relative
            if not generated.is_file():
                raise RuntimeError(f"Missing screenshot: {relative}")
            if not target.is_file() or digest(target) != digest(generated):
                changed.append(str(relative))
        for path, content in readmes.items():
            if path.read_text() != content:
                changed.append(path.name)
        if args.check:
            if changed:
                print("Screenshots need refreshing: " + ", ".join(changed))
                return 1
            print("Screenshot reproduction: PASS (all PNG bytes and README references match)")
            return 0
        output.mkdir(parents=True, exist_ok=True)
        for relative in paths:
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_suffix(".png.tmp")
            shutil.copyfile(work / "output" / relative, staging)
            staging.replace(target)
        for path, content in readmes.items():
            if path.read_text() != content:
                path.write_text(content)
        print(f"Updated {len(paths)} screenshots; {len(readmes)} READMEs use both themes")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Screenshot generation failed: {error}") from error
