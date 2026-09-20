import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shell_launchers_have_valid_syntax():
    for script in (
        REPO_ROOT / "clipper",
        REPO_ROOT / "engine" / "src" / "run_engine.sh",
        REPO_ROOT / "tools" / "run_flatpak_build_quiet.sh",
        REPO_ROOT / "tools" / "run_pytest_quiet.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)
