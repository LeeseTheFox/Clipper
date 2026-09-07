#!/usr/bin/env python3
"""Build Clipper's relocatable, multiarch obs-vkcapture game-side payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PAYLOAD_VERSION = "obs-vkcapture-1.5.6-clipper.1"
SOURCE_VERSION = "1.5.6"
SOURCE_COMMIT = "a9ea91fe1994708067e95d4159852b11b4209a16"
SOCKET_PREFIX = "/io/github/leesethefox/Clipper/vkcapture/"

ARCHITECTURES = {
    "x86_64": {
        "compiler": "cc",
        "flags": ("-march=x86-64", "-mtune=generic"),
        "linker_emulation": "elf_x86_64",
        "sysroot_libdirs": ("lib64", "usr/lib64"),
        "libdir": Path("lib64/clipper-gamecapture"),
    },
    "i386": {
        "compiler": "cc",
        "flags": ("-m32", "-march=i686", "-mtune=generic", "-mno-sse2"),
        "linker_emulation": "elf_i386",
        "sysroot_libdirs": ("lib", "usr/lib"),
        "libdir": Path("lib/clipper-gamecapture"),
    },
}

HOOKS = {
    "libVkLayer_clipper_vkcapture.so": (
        ("vklayer.c", "capture.c"),
        "vklayer.version",
        ("-lpthread", "-lrt"),
    ),
    "libclipper_glcapture.so": (
        ("dlsym.c", "elfhacks.c", "glinject.c", "capture.c"),
        "glinject.version",
        ("-ldl", "-lpthread", "-lrt"),
    ),
}


def run(arguments: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def command_output(arguments: list[str]) -> str:
    return subprocess.run(
        arguments, check=True, text=True, capture_output=True
    ).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_sysroot(rpm_dir: Path, destination: Path) -> None:
    rpms = sorted(rpm_dir.glob("*.rpm"))
    if len(rpms) != 8:
        raise RuntimeError(f"expected 8 pinned sysroot RPMs, found {len(rpms)}")
    destination.mkdir(parents=True)
    for rpm in rpms:
        run(["bsdtar", "-xf", str(rpm), "-C", str(destination)])


def prepare_include_overlay(source: Path, build: Path) -> Path:
    include = build / "include"
    include.mkdir(parents=True)
    for name in ("GL", "KHR", "vk_video", "vulkan"):
        system_header = Path("/usr/include") / name
        if not system_header.is_dir():
            raise RuntimeError(f"missing SDK headers: {system_header}")
        (include / name).symlink_to(system_header)
    (include / "plugin-macros.h").write_text(
        "#pragma once\n"
        "#define HAVE_X11_XCB 0\n"
        "#define HAVE_X11_XLIB 0\n"
        "#define HAVE_WAYLAND 0\n"
        f'#define PLUGIN_VERSION "{SOURCE_VERSION}"\n',
        encoding="utf-8",
    )
    return include


def compile_hooks(
    source: Path,
    build: Path,
    sysroot: Path,
    payload: Path,
    architecture: str,
    compiler_override: str | None,
) -> None:
    settings = ARCHITECTURES[architecture]
    compiler = compiler_override or str(settings["compiler"])
    compiler_include = Path(command_output([compiler, "-print-file-name=include"]))
    if not compiler_include.is_dir():
        raise RuntimeError(f"compiler include directory is missing: {compiler_include}")
    include = prepare_include_overlay(source, build)
    source_include = source / "src"
    output_dir = payload / settings["libdir"]
    output_dir.mkdir(parents=True)

    common = [
        compiler,
        f"--sysroot={sysroot}",
        "-std=gnu11",
        "-O2",
        "-fPIC",
        "-fno-plt",
        "-fstack-protector-strong",
        "-D_GNU_SOURCE",
        "-DNDEBUG",
        "-nostdinc",
        f"-ffile-prefix-map={source}=.",
        f"-ffile-prefix-map={build}=.",
        f"-I{include}",
        f"-I{source_include}",
        "-isystem",
        str(compiler_include),
        "-isystem",
        str(sysroot / "usr/include"),
        *settings["flags"],
    ]
    objects: dict[str, Path] = {}
    for source_name in sorted({name for hook in HOOKS.values() for name in hook[0]}):
        object_path = build / f"{source_name[:-2]}.o"
        run([*common, "-c", str(source_include / source_name), "-o", str(object_path)])
        objects[source_name] = object_path

    for filename, (source_names, version_script, libraries) in HOOKS.items():
        output = output_dir / filename
        linker_libraries = [library[2:] for library in libraries]
        run(
            [
                "ld",
                f"--sysroot={sysroot}",
                "-m",
                str(settings["linker_emulation"]),
                "--build-id=sha1",
                "-shared",
                "--as-needed",
                "-z",
                "defs",
                "-z",
                "relro",
                "-z",
                "now",
                f"--version-script={source_include / version_script}",
                *[str(objects[name]) for name in source_names],
                *[
                    argument
                    for relative in settings["sysroot_libdirs"]
                    for argument in ("-L", str(sysroot / relative))
                ],
                *[f"-l{library}" for library in linker_libraries],
                "-l:libgcc_s.so.1",
                "-lc",
                "-o",
                str(output),
            ]
        )
        run(["strip", "--strip-unneeded", str(output)])


def run_load_harness(
    sysroot: Path, payload: Path, build: Path, architecture: str
) -> None:
    settings = ARCHITECTURES[architecture]
    compiler = str(settings["compiler"])
    compiler_include = Path(command_output([compiler, "-print-file-name=include"]))
    build.mkdir(parents=True)
    source = build / "payload_dlopen_harness.c"
    source.write_text(
        "#include <dlfcn.h>\n"
        "#include <stdio.h>\n"
        "int main(int argc, char **argv) {\n"
        "    if (argc != 2) return 64;\n"
        "    void *handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);\n"
        "    if (!handle) { fprintf(stderr, \"%s\\n\", dlerror()); return 1; }\n"
        "    return dlclose(handle) == 0 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    obj = build / "payload_dlopen_harness.o"
    executable = build / "payload_dlopen_harness"
    run(
        [
            compiler,
            f"--sysroot={sysroot}",
            "-std=gnu11",
            "-O2",
            "-fno-stack-protector",
            "-nostdinc",
            *settings["flags"],
            "-isystem",
            str(compiler_include),
            "-isystem",
            str(sysroot / "usr/include"),
            "-c",
            str(source),
            "-o",
            str(obj),
        ]
    )
    libdirs = [sysroot / relative for relative in settings["sysroot_libdirs"]]
    interpreter = (
        "/lib64/ld-linux-x86-64.so.2"
        if architecture == "x86_64"
        else "/lib/ld-linux.so.2"
    )
    crt_dir = libdirs[-1]
    run(
        [
            "ld",
            f"--sysroot={sysroot}",
            "-m",
            str(settings["linker_emulation"]),
            "-dynamic-linker",
            interpreter,
            str(crt_dir / "crt1.o"),
            str(crt_dir / "crti.o"),
            str(obj),
            *[
                argument
                for directory in libdirs
                for argument in ("-L", str(directory))
            ],
            "-ldl",
            "-lc",
            str(crt_dir / "crtn.o"),
            "-o",
            str(executable),
        ]
    )
    loader = sysroot / interpreter.removeprefix("/")
    library_path = ":".join(str(directory) for directory in libdirs)
    for hook in HOOKS:
        run(
            [
                str(loader),
                "--library-path",
                library_path,
                str(executable),
                str(payload / settings["libdir"] / hook),
            ]
        )


def install_wrapper(payload: Path) -> None:
    wrapper = payload / "bin/clipper-gamecapture"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"$#\" -eq 0 ]; then\n"
        "    echo \"usage: clipper-gamecapture PROGRAM [ARG ...]\" >&2\n"
        "    exit 64\n"
        "fi\n"
        "script_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)\n"
        "payload_root=${script_dir%/bin}\n"
        "gl_hook=${payload_root}/\\$LIB/clipper-gamecapture/libclipper_glcapture.so\n"
        "export XDG_DATA_DIRS=${payload_root}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}\n"
        "export LD_PRELOAD=${gl_hook}${LD_PRELOAD:+:${LD_PRELOAD}}\n"
        "export CLIPPER_GAME_CAPTURE=1\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def install_vulkan_manifests(payload: Path) -> None:
    manifest_dir = payload / "share/vulkan/implicit_layer.d"
    manifest_dir.mkdir(parents=True)
    for architecture, suffix, library_dir in (
        ("x86_64", "64", "lib64"),
        ("i386", "32", "lib"),
    ):
        layer = {
            "file_format_version": "1.1.2",
            "layer": {
                "name": f"VK_LAYER_CLIPPER_vkcapture_{suffix}",
                "type": "GLOBAL",
                "library_path": (
                    f"../../../{library_dir}/clipper-gamecapture/"
                    "libVkLayer_clipper_vkcapture.so"
                ),
                "api_version": "1.3.221",
                "implementation_version": "1",
                "description": f"Clipper game capture ({architecture})",
                "functions": {
                    "vkNegotiateLoaderLayerInterfaceVersion": "OBS_Negotiate"
                },
                "enable_environment": {"CLIPPER_GAME_CAPTURE": "1"},
                "disable_environment": {"DISABLE_CLIPPER_GAME_CAPTURE": "1"},
            },
        }
        path = manifest_dir / f"clipper_vkcapture_{suffix}.json"
        path.write_text(json.dumps(layer, indent=2) + "\n", encoding="utf-8")


def install_aliases(payload: Path) -> dict[str, str]:
    aliases = {
        Path("lib/x86_64-linux-gnu/clipper-gamecapture"): (
            "../../lib64/clipper-gamecapture"
        ),
        Path("lib/i386-linux-gnu/clipper-gamecapture"): "../clipper-gamecapture",
    }
    for relative, target in aliases.items():
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    return {path.as_posix(): target for path, target in aliases.items()}


def write_payload_manifest(payload: Path, aliases: dict[str, str]) -> None:
    files = {
        path.relative_to(payload).as_posix(): sha256(path)
        for path in sorted(payload.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.name != "manifest.json"
    }
    manifest = {
        "schema": 1,
        "payload_version": PAYLOAD_VERSION,
        "source": {
            "project": "obs-vkcapture",
            "version": SOURCE_VERSION,
            "commit": SOURCE_COMMIT,
            "patch": "clipper-ipc-v1",
        },
        "abi": {
            "maximum_glibc": "2.17",
            "x86_64_cpu": "x86-64",
            "i386_cpu": "i686",
        },
        "ipc": {
            "kind": "abstract-unix",
            "socket_prefix": SOCKET_PREFIX,
            "same_uid_only": True,
        },
        "files": files,
        "links": aliases,
    }
    (payload / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--rpm-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--cc-x86-64")
    parser.add_argument("--cc-i386")
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    rpm_dir = args.rpm_dir.resolve(strict=True)
    destination = args.destination.resolve()
    build = args.build_dir.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    if build.exists():
        shutil.rmtree(build)
    destination.mkdir(parents=True)
    build.mkdir(parents=True)

    patched_sources = (source / "src/capture.c", source / "src/vkcapture.c")
    for path in patched_sources:
        text = path.read_text(encoding="utf-8")
        if SOCKET_PREFIX not in text or "/com/obsproject/vkcapture" in text:
            raise RuntimeError(f"Clipper IPC patch is not active in {path}")

    sysroot = build / "sysroot"
    prepare_sysroot(rpm_dir, sysroot)
    compile_hooks(
        source, build / "x86_64", sysroot, destination, "x86_64", args.cc_x86_64
    )
    compile_hooks(source, build / "i386", sysroot, destination, "i386", args.cc_i386)
    run_load_harness(sysroot, destination, build / "load-x86_64", "x86_64")
    run_load_harness(sysroot, destination, build / "load-i386", "i386")
    install_wrapper(destination)
    install_vulkan_manifests(destination)
    license_dir = destination / "licenses"
    license_dir.mkdir()
    shutil.copy2(source / "LICENSE", license_dir / "obs-vkcapture-GPL-2.0.txt")
    aliases = install_aliases(destination)
    write_payload_manifest(destination, aliases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
