"""Exercise failure handling in the installed-engine capture smoke tool."""

from io import BytesIO

import pytest

from tools.game_capture_frame_smoke import receive_until


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
