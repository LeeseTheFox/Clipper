"""Exercise failure handling in the installed-engine capture smoke tool."""

from io import BytesIO
from types import SimpleNamespace

import pytest

from tools.game_capture_frame_smoke import receive_until, wait_for_hook


def test_receive_skips_events_and_waits_for_save_completion():
    stream = BytesIO(
        b'{"event":"status_changed"}\n{"ok":true}\n'
        b'{"event":"clip_saved","path":"/tmp/clip.mkv"}\n'
    )
    assert receive_until(stream, "event", "clip_saved")["path"] == "/tmp/clip.mkv"


@pytest.mark.parametrize("reply", [
    b'{"ok":false,"error":"cannot start replay"}\n',
    b'{"ok":true}\n{"event":"clip_save_failed","error":"disk full"}\n',
    b"",
])
def test_receive_reports_failures_without_waiting_for_timeout(reply):
    with pytest.raises(RuntimeError, match="engine (request failed|disconnected)"):
        receive_until(BytesIO(reply), "event", "clip_saved")


def test_hook_event_does_not_leave_status_reply_in_front_of_preview():
    stream = BytesIO(
        b'{"event":"status_changed","game_hooked":true,"buffer_active":true}\n'
        b'{"ok":true,"game_hooked":true,"buffer_active":true}\n'
        b'{"ok":true,"format":"BGRA"}\n'
    )
    client = SimpleNamespace(sendall=lambda _data: None)
    assert wait_for_hook(client, stream, SimpleNamespace())["game_hooked"]
    assert receive_until(stream, "ok")["format"] == "BGRA"
