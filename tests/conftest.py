"""Shared support for compiling the implementations shipped in OBS patches."""

import subprocess
from pathlib import Path

import pytest

from tools.generate_native_test_headers import generate_header

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_native_test(tmp_path):
    def run(source, header, *flags):
        generate_header(tmp_path, header)
        binary = tmp_path / Path(source).stem
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", *flags,
             "-I", str(tmp_path), str(ROOT / "tests/native" / source), "-o", str(binary)],
            check=True, capture_output=True, text=True,
        )
        return subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    return run
