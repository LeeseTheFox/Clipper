#!/usr/bin/env python3
"""Validate Clipper's receiver and relocatable multiarch capture payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

PAYLOAD_VERSION = "obs-vkcapture-1.5.6-clipper.3"
SOURCE_VERSION = "1.5.6"
SOURCE_COMMIT = "a9ea91fe1994708067e95d4159852b11b4209a16"
SOCKET_PREFIX = "/io/github/leesethefox/Clipper/vkcapture/"
MAX_GLIBC = (2, 17)

HELPERS = (
    Path("libexec/clipper/clipper-monitor-host"),
)
PAYLOAD = Path("share/clipper/game-capture/payloads") / PAYLOAD_VERSION
CLIPPER_RECEIVER_CANDIDATES = (
    Path("lib/obs-plugins/linux-vkcapture.so"),
    Path("lib64/obs-plugins/linux-vkcapture.so"),
)
ELF_FILES = {
    Path("lib64/clipper-gamecapture/libclipper_glcapture.so"): (
        "ELF64",
        "Advanced Micro Devices X86-64",
    ),
    Path("lib64/clipper-gamecapture/libVkLayer_clipper_vkcapture.so"): (
        "ELF64",
        "Advanced Micro Devices X86-64",
    ),
    Path("lib/clipper-gamecapture/libclipper_glcapture.so"): (
        "ELF32",
        "Intel 80386",
    ),
    Path("lib/clipper-gamecapture/libVkLayer_clipper_vkcapture.so"): (
        "ELF32",
        "Intel 80386",
    ),
}
ALLOWED_NEEDED = {
    "libc.so.6",
    "libdl.so.2",
    "libgcc_s.so.1",
    "libpthread.so.0",
    "librt.so.1",
}
EXPECTED_LINKS = {
    Path("lib/x86_64-linux-gnu/clipper-gamecapture"): (
        "../../lib64/clipper-gamecapture"
    ),
    Path("lib/i386-linux-gnu/clipper-gamecapture"): "../clipper-gamecapture",
}


class ValidationError(RuntimeError):
    pass


def _run_tool(program: str, path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [program, *arguments, str(path)],
        check=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise ValidationError(
            f"{program} failed for {path}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _readelf(path: Path, *arguments: str) -> str:
    return _run_tool("readelf", path, *arguments)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _glibc_versions(path: Path) -> set[tuple[int, ...]]:
    return {
        tuple(int(part) for part in match.split("."))
        for match in re.findall(
            r"GLIBC_(\d+(?:\.\d+)+)", _readelf(path, "--version-info")
        )
    }


def _validate_helper(root: Path, relative: Path) -> str:
    path = root / relative
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValidationError(f"host helper is not a regular file: {relative}")
    if info.st_mode & 0o111 == 0:
        raise ValidationError(f"host helper is not executable: {relative}")
    header = _readelf(path, "-h")
    if "Class:                             ELF64" not in header:
        raise ValidationError(f"host helper is not ELFCLASS64: {relative}")
    if "Machine:                           Advanced Micro Devices X86-64" not in header:
        raise ValidationError(f"host helper has unexpected machine type: {relative}")

    program_headers = _readelf(path, "-l")
    dynamic = _readelf(path, "-d")
    if "INTERP" in program_headers or "NEEDED" in dynamic:
        maximum: tuple[int, ...] = max(_glibc_versions(path), default=(0,))
        if maximum > (2, 31):
            version = ".".join(str(part) for part in maximum)
            raise ValidationError(
                f"{relative} imports GLIBC_{version}, above the GLIBC_2.31 baseline"
            )
        linkage = "dynamic-glibc<=2.31"
    else:
        linkage = "static"
    raw = path.read_bytes()
    if b"/app/" in raw or b"build-dir" in raw or b".flatpak-builder" in raw:
        raise ValidationError(f"host helper embeds a sandbox/build path: {relative}")
    return linkage


def _find_receiver(root: Path, candidates: tuple[Path, ...], label: str) -> Path:
    receiver = next((root / item for item in candidates if (root / item).is_file()), None)
    if receiver is None:
        raise ValidationError(f"bundled {label} receiver is missing")
    return receiver


def _validate_clipper_receiver(path: Path) -> None:
    raw = path.read_bytes()
    if SOCKET_PREFIX.encode() not in raw:
        raise ValidationError("Clipper receiver lacks its private socket prefix")
    if b"/com/obsproject/vkcapture" in raw:
        raise ValidationError("Clipper receiver still contains the upstream socket name")
    if b"different uid" not in raw:
        raise ValidationError("Clipper receiver does not enforce same-UID peers")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} is not a JSON object")
    return value


def _payload_paths(payload: Path) -> set[Path]:
    paths: set[Path] = set()
    for directory, subdirectories, filenames in os.walk(payload, followlinks=False):
        base = Path(directory)
        for name in subdirectories:
            path = base / name
            if path.is_symlink():
                paths.add(path.relative_to(payload))
        for name in filenames:
            paths.add((base / name).relative_to(payload))
    return paths


def _validate_payload_manifest(payload: Path) -> dict[str, Any]:
    manifest = _load_json(payload / "manifest.json", "payload manifest")
    expected_fields = {"schema", "payload_version", "source", "abi", "ipc", "files", "links"}
    if set(manifest) != expected_fields or manifest["schema"] != 1:
        raise ValidationError("payload manifest schema or fields are invalid")
    if manifest["payload_version"] != PAYLOAD_VERSION:
        raise ValidationError("payload version does not match its versioned directory")
    if manifest["source"] != {
        "project": "obs-vkcapture",
        "version": SOURCE_VERSION,
        "commit": SOURCE_COMMIT,
        "patch": "clipper-ipc-v1-copy-pacing-v2",
    }:
        raise ValidationError("payload source revision or patch identifier is invalid")
    if manifest["abi"] != {
        "maximum_glibc": "2.17",
        "x86_64_cpu": "x86-64",
        "i386_cpu": "i686",
    }:
        raise ValidationError("payload ABI declaration is invalid")
    if manifest["ipc"] != {
        "kind": "abstract-unix",
        "socket_prefix": SOCKET_PREFIX,
        "same_uid_only": True,
    }:
        raise ValidationError("payload IPC declaration is invalid")
    if not isinstance(manifest["files"], dict) or not isinstance(manifest["links"], dict):
        raise ValidationError("payload file and link inventories must be objects")
    return manifest


def _validate_payload_inventory(payload: Path, manifest: dict[str, Any]) -> None:
    files = manifest["files"]
    links = manifest["links"]
    expected_links = {path.as_posix(): target for path, target in EXPECTED_LINKS.items()}
    if links != expected_links:
        raise ValidationError("payload alias inventory is invalid")

    expected_paths = {Path("manifest.json"), *map(Path, files), *map(Path, links)}
    actual_paths = _payload_paths(payload)
    if actual_paths != expected_paths:
        missing = sorted(path.as_posix() for path in expected_paths - actual_paths)
        extra = sorted(path.as_posix() for path in actual_paths - expected_paths)
        raise ValidationError(f"payload inventory mismatch; missing={missing}, extra={extra}")

    for relative, expected_hash in files.items():
        path = payload / relative
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"payload file is not regular: {relative}")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValidationError(f"invalid SHA-256 in payload manifest: {relative}")
        if _sha256(path) != expected_hash:
            raise ValidationError(f"payload SHA-256 mismatch: {relative}")

    for relative, expected_target in links.items():
        path = payload / relative
        if not path.is_symlink() or os.readlink(path) != expected_target:
            raise ValidationError(f"payload alias is invalid: {relative}")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(payload.resolve())
        except ValueError as exc:
            raise ValidationError(f"payload alias escapes the payload: {relative}") from exc


def _validate_payload_elf(payload: Path, relative: Path, elf_class: str, machine: str) -> None:
    path = payload / relative
    header = _readelf(path, "-h")
    if f"Class:                             {elf_class}" not in header:
        raise ValidationError(f"wrong ELF class: {relative}")
    if f"Machine:                           {machine}" not in header:
        raise ValidationError(f"wrong ELF machine: {relative}")

    dynamic = _readelf(path, "-d")
    needed = set(re.findall(r"Shared library: \[([^]]+)]", dynamic))
    unexpected = needed - ALLOWED_NEEDED
    if unexpected:
        raise ValidationError(f"unexpected NEEDED libraries in {relative}: {sorted(unexpected)}")
    if "libc.so.6" not in needed:
        raise ValidationError(f"libc dependency missing from {relative}")

    maximum: tuple[int, ...] = max(_glibc_versions(path), default=(0,))
    if maximum > MAX_GLIBC:
        version = ".".join(str(part) for part in maximum)
        raise ValidationError(f"{relative} imports GLIBC_{version}, above GLIBC_2.17")

    notes = _readelf(path, "--notes")
    if not re.search(r"Build ID: [0-9a-f]{40}", notes):
        raise ValidationError(f"{relative} lacks a diagnostic GNU build ID")
    isa_lines = [line for line in notes.splitlines() if "x86 ISA needed:" in line]
    if any("x86-64-baseline" not in line for line in isa_lines):
        raise ValidationError(f"{relative} requires a non-baseline x86 ISA")

    raw = path.read_bytes()
    forbidden = (b"/app/", b"/run/build/", b"/home/", b"/var/home/", b"build-dir")
    if any(value in raw for value in forbidden):
        raise ValidationError(f"{relative} embeds a build or developer path")
    if SOCKET_PREFIX.encode() not in raw or b"/com/obsproject/vkcapture" in raw:
        raise ValidationError(f"{relative} does not use Clipper's private socket")


def _validate_vulkan_manifests(payload: Path) -> None:
    for suffix, library_dir in (("64", "lib64"), ("32", "lib")):
        relative = Path(f"share/vulkan/implicit_layer.d/clipper_vkcapture_{suffix}.json")
        data = _load_json(payload / relative, f"Vulkan {suffix}-bit manifest")
        layer = data.get("layer")
        if not isinstance(layer, dict):
            raise ValidationError(f"Vulkan {suffix}-bit layer object is missing")
        expected_library = (
            f"../../../{library_dir}/clipper-gamecapture/"
            "libVkLayer_clipper_vkcapture.so"
        )
        if layer.get("library_path") != expected_library or Path(expected_library).is_absolute():
            raise ValidationError(f"Vulkan {suffix}-bit library path is not relocatable")
        if layer.get("enable_environment") != {"CLIPPER_GAME_CAPTURE": "1"}:
            raise ValidationError(f"Vulkan {suffix}-bit enable variable is invalid")
        if layer.get("disable_environment") != {"DISABLE_CLIPPER_GAME_CAPTURE": "1"}:
            raise ValidationError(f"Vulkan {suffix}-bit disable variable is invalid")
        resolved = (payload / relative.parent / expected_library).resolve(strict=True)
        try:
            resolved.relative_to(payload.resolve())
        except ValueError as exc:
            raise ValidationError(f"Vulkan {suffix}-bit library path escapes payload") from exc


def _validate_wrapper(payload: Path) -> None:
    path = payload / "bin/clipper-gamecapture"
    info = path.stat()
    if info.st_mode & 0o111 == 0:
        raise ValidationError("payload wrapper is not executable")
    wrapper = path.read_text(encoding="utf-8")
    required = (
        "#!/bin/sh",
        "CLIPPER_GAME_CAPTURE=1",
        "XDG_DATA_DIRS=${payload_root}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}",
        "gl_hook=${payload_root}/\\$LIB/clipper-gamecapture/libclipper_glcapture.so",
        "LD_PRELOAD=${gl_hook}${LD_PRELOAD:+:${LD_PRELOAD}}",
        'exec "$@"',
    )
    if any(fragment not in wrapper for fragment in required):
        raise ValidationError("payload wrapper does not preserve the required launch contract")
    forbidden = ("eval ", "/app/", "/usr/lib", "obs-gamecapture", "exec $@")
    if any(fragment in wrapper for fragment in forbidden):
        raise ValidationError("payload wrapper contains an unsafe or non-relocatable construct")


def _validate_payload(payload: Path) -> None:
    manifest = _validate_payload_manifest(payload)
    _validate_payload_inventory(payload, manifest)
    required_files = {
        *ELF_FILES,
        Path("bin/clipper-gamecapture"),
        Path("share/vulkan/implicit_layer.d/clipper_vkcapture_64.json"),
        Path("share/vulkan/implicit_layer.d/clipper_vkcapture_32.json"),
        Path("licenses/obs-vkcapture-GPL-2.0.txt"),
    }
    if not required_files.issubset(map(Path, manifest["files"])):
        raise ValidationError("payload manifest omits a mandatory file")
    for relative, (elf_class, machine) in ELF_FILES.items():
        _validate_payload_elf(payload, relative, elf_class, machine)
    _validate_vulkan_manifests(payload)
    _validate_wrapper(payload)


def validate(deployment: Path) -> list[str]:
    deployment = deployment.resolve(strict=True)
    report = []
    for helper in HELPERS:
        report.append(f"{helper}: {_validate_helper(deployment, helper)}")
    clipper_receiver = _find_receiver(
        deployment, CLIPPER_RECEIVER_CANDIDATES, "Clipper"
    )
    _validate_clipper_receiver(clipper_receiver)
    report.append(f"Clipper receiver: {clipper_receiver.relative_to(deployment)}")

    _validate_payload(deployment / PAYLOAD)
    report.append(f"portable payload: {PAYLOAD_VERSION} (x86_64+i386, Vulkan+OpenGL)")
    report.append("payload ABI: GLIBC <= 2.17, baseline x86 CPUs")
    return report


def _copy_validation_tree(deployment: Path, destination: Path) -> None:
    for relative in HELPERS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(deployment / relative, target)
    for candidates in (CLIPPER_RECEIVER_CANDIDATES,):
        source = next(deployment / item for item in candidates if (deployment / item).is_file())
        target = destination / source.relative_to(deployment)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(deployment / PAYLOAD, destination / PAYLOAD, symlinks=True)


def _rewrite_hash(payload: Path, relative: Path) -> None:
    manifest_path = payload / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][relative.as_posix()] = _sha256(payload / relative)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _run_damaged_fixture(
    deployment: Path, name: str, damage: Callable[[Path], None]
) -> str:
    with tempfile.TemporaryDirectory(prefix=f"clipper-payload-{name}-") as temporary:
        fixture = Path(temporary)
        _copy_validation_tree(deployment, fixture)
        damage(fixture / PAYLOAD)
        try:
            validate(fixture)
        except (OSError, ValidationError):
            return name
        raise ValidationError(f"damaged fixture unexpectedly passed: {name}")


def self_test(deployment: Path) -> list[str]:
    deployment = deployment.resolve(strict=True)

    def tamper_wrapper(payload: Path) -> None:
        with (payload / "bin/clipper-gamecapture").open("a", encoding="utf-8") as stream:
            stream.write("# tampered\n")

    def remove_library(payload: Path) -> None:
        (payload / "lib/clipper-gamecapture/libclipper_glcapture.so").unlink()

    def absolute_vulkan_path(payload: Path) -> None:
        relative = Path("share/vulkan/implicit_layer.d/clipper_vkcapture_64.json")
        data = json.loads((payload / relative).read_text(encoding="utf-8"))
        data["layer"]["library_path"] = "/tmp/not-relocatable.so"
        (payload / relative).write_text(json.dumps(data), encoding="utf-8")
        _rewrite_hash(payload, relative)

    def wrong_elf_machine(payload: Path) -> None:
        relative = Path("lib/clipper-gamecapture/libclipper_glcapture.so")
        shutil.copy2(
            payload / "lib64/clipper-gamecapture/libclipper_glcapture.so",
            payload / relative,
        )
        _rewrite_hash(payload, relative)

    def wrong_source(payload: Path) -> None:
        path = payload / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source"]["commit"] = "0" * 40
        path.write_text(json.dumps(data), encoding="utf-8")

    fixtures = (
        ("changed-hash", tamper_wrapper),
        ("missing-file", remove_library),
        ("absolute-vulkan-path", absolute_vulkan_path),
        ("wrong-elf-machine", wrong_elf_machine),
        ("wrong-source", wrong_source),
    )
    return [_run_damaged_fixture(deployment, name, damage) for name, damage in fixtures]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "deployment",
        nargs="?",
        type=Path,
        default=Path("build-dir-bounded/files"),
        help="Flatpak deployment files directory",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        lines = validate(args.deployment)
        if args.self_test:
            fixtures = self_test(args.deployment)
            lines.append(f"damaged fixtures rejected: {', '.join(fixtures)}")
    except (OSError, ValidationError, subprocess.SubprocessError) as exc:
        print(f"Game capture payload validation: FAIL\n{exc}", file=sys.stderr)
        return 1
    output = "Game capture payload validation: PASS\n" + "\n".join(
        f"- {line}" for line in lines
    )
    print(output)
    if args.report is not None:
        args.report.write_text(output + "\n", encoding="utf-8")
        os.chmod(args.report, 0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
