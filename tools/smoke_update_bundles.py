#!/usr/bin/env python3
"""Exercise bundle replacement and host relaunch in an isolated user installation.

Requires org.gnome.Platform//50 in a system installation. Never updates Clipper.
"""

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def main():
    app_id = "io.github.leesethefox.Clipper.UpdateSmoke" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="clipper-update-smoke-") as temporary:
        root = Path(temporary)
        environment = {**os.environ, "FLATPAK_USER_DIR": str(root / "installation")}

        def run(*args):
            result = subprocess.run(
                args, env=environment, capture_output=True, text=True, check=False, timeout=60
            )
            if result.returncode:
                raise RuntimeError(f"{args[0:2]}: {result.stderr.strip()}")
            return result.stdout.strip()

        build = root / "build"
        files = build / "files"
        (files / "bin").mkdir(parents=True)
        (files / "share/metainfo").mkdir(parents=True)
        (build / "metadata").write_text(
            f"[Application]\nname={app_id}\nruntime=org.gnome.Platform/x86_64/50\n"
            "command=update-smoke\n[Context]\n"
            f"filesystems={root};\n[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n"
        )
        launcher = files / "bin/update-smoke"
        launcher.write_text(f"#!/bin/sh\ncat /app/version > {root}/restarted\n")
        launcher.chmod(0o755)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "ui/updates.py", files / "updates.py")
        run("flatpak", "build-finish", str(build))
        setup = (
            "import sys, os, shutil; from pathlib import Path; "
            "sys.path.insert(0, '/app'); import updates; "
            f"updates.APP_ID = {app_id!r}; "
            "updates.host_command = lambda *args: "
            "['flatpak-spawn', '--host', '--directory=/', 'env', "
            f"'FLATPAK_USER_DIR={root}/installation', *args]; "
        )
        try:
            for version in ("1.0.0", "1.1.0"):
                (files / "version").write_text(version)
                (files / f"share/metainfo/{app_id}.metainfo.xml").write_text(
                    f'<component><id>{app_id}</id><releases><release version="{version}"/>'
                    "</releases></component>"
                )
                run("flatpak", "build-export", "--disable-sandbox", str(root / "repo"), str(build))
                bundle = root / f"{version}.flatpak"
                run("flatpak", "build-bundle", str(root / "repo"), str(bundle), app_id)
                if version == "1.0.0":
                    run(
                        "flatpak",
                        "--user",
                        "remote-add",
                        "--no-gpg-verify",
                        "clipper-smoke-local",
                        str(root / "repo"),
                    )
                    run(
                        "flatpak",
                        "--user",
                        "install",
                        "--assumeyes",
                        "--no-deps",
                        "clipper-smoke-local",
                        app_id,
                    )
                else:
                    commit = run(
                        "flatpak",
                        "--user",
                        "remote-info",
                        "--show-commit",
                        "clipper-smoke-local",
                        app_id,
                    )
                    code = setup + (
                        "bundle = Path(os.environ['XDG_CACHE_HOME']) / 'update.flatpak'; "
                        f"shutil.copyfile({str(bundle)!r}, bundle); "
                        f"release = updates.Release({version!r}, '', 0, '', "
                        f"'app/{app_id}/x86_64/master', {commit!r}, ''); "
                        "installation = updates.running_installation(); "
                        "assert installation == updates.Installation('--user', 'x86_64', 'master'); "
                        "updates.install(release, installation, bundle); "
                        "assert Path('/app/version').read_text() == '1.0.0'"
                    )
                    run("flatpak", "--user", "run", "--command=python3", app_id, "-c", code)
                    print("PASS updater replaced the deployment while the old sandbox kept running")
                actual = run("flatpak", "--user", "run", "--command=cat", app_id, "/app/version")
                assert actual == version, (actual, version)
                print(f"PASS installed and launched bundle {version}")

            code = setup + (
                "updates.restart_after_exit(updates.Installation('--user', 'x86_64', 'master'))"
            )
            run("flatpak", "--user", "run", "--command=python3", app_id, "-c", code)
            deadline = time.monotonic() + 15
            while not (root / "restarted").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert (root / "restarted").read_text() == "1.1.0"
            print("PASS host helper relaunched a fresh sandbox after the old instance exited")
        finally:
            # Only this randomly named smoke app and its temporary installation.
            subprocess.run(
                ["flatpak", "--user", "uninstall", "--assumeyes", "--delete-data", app_id],
                env=environment,
                capture_output=True,
                timeout=30,
            )


if __name__ == "__main__":
    main()
