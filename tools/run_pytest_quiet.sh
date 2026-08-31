#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
venv_python="$repo_root/venv/bin/python"
venv_pytest="$repo_root/venv/bin/pytest"
venv_pyright="$repo_root/venv/bin/pyright"
venv_ruff="$repo_root/venv/bin/ruff"

if [[ ! -x "$venv_pytest" || ! -x "$venv_python" || ! -x "$venv_pyright" || ! -x "$venv_ruff" ]]; then
    echo "error: expected local virtualenv tools at ./venv/bin/{python,pytest,pyright,ruff}" >&2
    exit 1
fi

if [[ $# -eq 0 ]]; then
    set -- tests
fi

log_file="$(mktemp)"
xml_file="$(mktemp --suffix=.xml)"
pyright_log_file="$(mktemp)"
ruff_log_file="$(mktemp)"
cleanup() {
    rm -f "$log_file" "$xml_file" "$pyright_log_file" "$ruff_log_file"
}
trap cleanup EXIT

pushd "$repo_root" >/dev/null
set +e
CLIPPER_AGENT_TEST_MODE=1 "$venv_pytest" "$@" -q --junitxml="$xml_file" >"$log_file" 2>&1
pytest_status=$?
"$venv_pyright" >"$pyright_log_file" 2>&1
pyright_status=$?
"$venv_ruff" check ui tests >"$ruff_log_file" 2>&1
ruff_status=$?
set -e
popd >/dev/null

set +e
"$venv_python" - "$xml_file" "$pytest_status" "$log_file" <<'PY'
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

xml_path = Path(sys.argv[1])
pytest_status = int(sys.argv[2])
log_path = Path(sys.argv[3])


def first_meaningful_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


failures: list[tuple[str, str, str]] = []
summary: dict[str, int] | None = None
parse_error = None

if xml_path.exists() and xml_path.stat().st_size > 0:
    try:
        root = ET.parse(xml_path).getroot()
        testsuite = root if root.tag == "testsuite" else root.find("testsuite")
        if testsuite is not None:
            tests = int(testsuite.attrib.get("tests", "0"))
            failed = int(testsuite.attrib.get("failures", "0"))
            errors = int(testsuite.attrib.get("errors", "0"))
            skipped = int(testsuite.attrib.get("skipped", "0"))
            passed = tests - failed - errors - skipped
            summary = {
                "tests": tests,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": skipped,
            }

            for case in testsuite.iter("testcase"):
                case_id = f'{case.attrib.get("classname", "")}::{case.attrib.get("name", "")}'.strip(":")
                for tag in ("failure", "error"):
                    node = case.find(tag)
                    if node is not None:
                        headline = node.attrib.get("message") or first_meaningful_line(node.text)
                        detail = first_meaningful_line(node.text)
                        failures.append((case_id, tag, headline or detail or "no failure reason reported"))
    except ET.ParseError as exc:
        parse_error = f"could not parse junit xml: {exc}"

if summary is None:
    print("Pytest summary unavailable.")
    if parse_error:
        print(f"Reason: {parse_error}")
    log_text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    if log_text:
        print()
        print(log_text)
    sys.exit(pytest_status)

overall = "PASS" if pytest_status == 0 else "FAIL"
print(f"Pytest result: {overall}")
print(
    "Summary: "
    f'{summary["passed"]} passed, '
    f'{summary["skipped"]} skipped, '
    f'{summary["failed"]} failed, '
    f'{summary["errors"]} errors, '
    f'{summary["tests"]} total'
)

if failures:
    print()
    print("Failed tests:")
    for case_id, tag, reason in failures:
        print(f"- {case_id} [{tag}]: {reason}")

if pytest_status != 0 and not failures:
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    trimmed = log_text.strip()
    if trimmed:
        print()
        print("Pytest output:")
        print(trimmed)

sys.exit(pytest_status)
PY
pytest_summary_status=$?
set -e

if [[ "$pyright_status" -eq 0 ]]; then
    echo "Pyright result: PASS"
else
    echo "Pyright result: FAIL"
    cat "$pyright_log_file"
fi

if [[ "$ruff_status" -eq 0 ]]; then
    echo "Ruff result: PASS"
else
    echo "Ruff result: FAIL"
    cat "$ruff_log_file"
fi

if [[ "$pytest_summary_status" -ne 0 || "$pyright_status" -ne 0 || "$ruff_status" -ne 0 ]]; then
    exit 1
fi
