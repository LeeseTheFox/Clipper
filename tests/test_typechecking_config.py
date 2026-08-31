import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pyright_checks_production_and_test_code_in_standard_mode():
    config = json.loads(
        (REPO_ROOT / "pyrightconfig.json").read_text(encoding="utf-8")
    )

    assert config["include"] == ["ui", "tests"]
    assert config["typeCheckingMode"] == "standard"
    assert "[tool.pyright]" not in (REPO_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_standard_python_check_runs_pyright_and_ruff():
    check_script = (REPO_ROOT / "tools" / "run_pytest_quiet.sh").read_text(
        encoding="utf-8"
    )

    assert 'venv_pyright="$repo_root/venv/bin/pyright"' in check_script
    assert '"$venv_pyright" >"$pyright_log_file"' in check_script
    assert 'venv_ruff="$repo_root/venv/bin/ruff"' in check_script
    assert '"$venv_ruff" check ui tests' in check_script
