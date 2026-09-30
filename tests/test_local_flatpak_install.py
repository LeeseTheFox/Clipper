"""Exercise the installer with a stateful Flatpak command substitute.

Real origin selection is checked separately in an isolated Flatpak installation.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def local_installer(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "install_flatpak_quiet.sh"
    shutil.copyfile(REPO_ROOT / "tools/install_flatpak_quiet.sh", script)
    commands = tmp_path / "commands"
    commands.mkdir()
    state_path = tmp_path / "state.json"
    data = tmp_path / "settings.json"
    data.write_text('{"games": ["preserve me"]}')
    command = commands / "flatpak"
    command.write_text('#!/bin/sh\nexec "$INSTALL_PYTHON" "$INSTALL_FAKE" "$@"\n')
    command.chmod(0o755)
    fake = commands / "flatpak.py"
    fake.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ['INSTALL_STATE'])
state = json.loads(state_path.read_text())
operation = next(a for a in args if not a.startswith('--'))
state['operations'].append(operation)
status = 0
if operation == 'build-bundle':
    status = int(state.get('failure') == 'export')
    if not status:
        positional = [a for a in args if not a.startswith('--')]
        Path(positional[2]).write_text(json.dumps({
            'url': '' if '--repo-url=' in args else None
        }))
elif operation == 'install':
    bundle = json.loads(Path(args[-1]).read_text())
    if '--no-deploy' in args:
        status = int(state.get('failure') == 'prepare')
        # Explicit URL metadata reconfigures even an already installed origin.
        if bundle['url'] is not None:
            state['url'] = bundle['url']
            state['repository_url'] = bundle['url']
    elif state.get('failure') == 'install':
        status = 1
    else:
        if not state['installed']:
            state['url'] = bundle['url'] or ''
        state['installed'] = True
elif operation == 'info':
    if state['installed']:
        print('clipper-origin')
    else:
        status = 1
elif operation == 'remotes':
    print('clipper-origin\\t' + state['url'])
elif operation == 'ps':
    if state['running']:
        print('instance io.github.leesethefox.Clipper')
elif operation == 'kill':
    state['running'] = False
elif operation == 'uninstall':
    state['installed'] = False
    state['url'] = ''
    if '--delete-data' in args:
        Path(os.environ['INSTALL_DATA']).unlink()
else:
    raise AssertionError(args)
state_path.write_text(json.dumps(state))
sys.exit(status)
"""
    )
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "INSTALL_STATE": str(state_path),
        "INSTALL_DATA": str(data),
        "INSTALL_PYTHON": sys.executable,
        "INSTALL_FAKE": str(fake),
        "TMPDIR": str(tmp_path),
    }

    def run(state):
        state.setdefault('repository_url', state['url'])
        state_path.write_text(json.dumps(state))
        result = subprocess.run(
            ["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10
        )
        return result, json.loads(state_path.read_text()), data

    return run


@pytest.mark.parametrize("origin", [None, "", "file:///build/repo"])
def test_local_bundle_install_retains_data_and_has_no_repository_update_source(
    local_installer, origin
):
    result, state, data = local_installer(
        {"installed": origin is not None, "url": origin or "", "running": True, "operations": []}
    )
    assert result.returncode == 0, result.stderr
    assert state["installed"] and not state["running"]
    assert state["url"] == ""
    assert state["repository_url"] == (origin or "")
    assert json.loads(data.read_text()) == {"games": ["preserve me"]}
    # Only a repository origin needs the data-preserving migration.
    assert state["operations"].count("uninstall") == int(bool(origin))
    result, state, data = local_installer({**state, "operations": []})
    assert result.returncode == 0, result.stderr
    assert state["installed"] and state["url"] == ""
    assert "uninstall" not in state["operations"]
    assert data.exists()


@pytest.mark.parametrize("failure", ["export", "prepare"])
def test_failed_bundle_preparation_leaves_the_running_installation_untouched(
    local_installer, failure
):
    initial = {
        "installed": True, "url": "file:///build/repo", "running": True,
        "operations": [], "failure": failure,
    }
    result, state, data = local_installer(initial)
    assert result.returncode != 0
    assert state["installed"] and state["running"] and state["url"] == initial["url"]
    assert data.exists()
    assert state["operations"] == (
        ["build-bundle"] if failure == "export" else ["build-bundle", "install"]
    )


def test_failed_install_after_origin_migration_retains_data_for_retry(local_installer):
    result, state, data = local_installer(
        {"installed": True, "url": "file:///build/repo", "running": True,
         "operations": [], "failure": "install"}
    )
    assert result.returncode != 0
    assert not state["installed"]
    assert json.loads(data.read_text()) == {"games": ["preserve me"]}
    result, state, data = local_installer({**state, "failure": "", "operations": []})
    assert result.returncode == 0, result.stderr
    assert state["installed"] and state["url"] == ""
    assert json.loads(data.read_text()) == {"games": ["preserve me"]}
