"""Run the portable hook scheduler without a graphics driver."""
import subprocess
from pathlib import Path


def test_copy_pacing_rates_lifecycle_and_wire_format(tmp_path):
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "copy-pacing"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
        str(root / "engine/gamecapture/test_copy_pacing.c"), "-o", str(executable),
    ], check=True, capture_output=True)
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
    assert "copy pacing tests passed" in result.stdout
