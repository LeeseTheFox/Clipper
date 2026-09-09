import pytest

pytest.importorskip("gi")

from engine_client import STATE_CONNECTED, STATE_DISCONNECTED, EngineClient


def test_engine_client_allows_multiple_state_callbacks():
    client = EngineClient("unused.sock")
    seen = []

    client.on_state_change(lambda state: seen.append(("first", state)))
    client.on_state_change(lambda state: seen.append(("second", state)))

    client._set_state(STATE_CONNECTED)

    assert seen == [
        ("first", STATE_CONNECTED),
        ("second", STATE_CONNECTED),
    ]


def test_engine_client_state_callback_clear():
    client = EngineClient("unused.sock")
    seen = []

    client.on_state_change(lambda state: seen.append(state))
    client.on_state_change(None)

    client._set_state(STATE_CONNECTED)
    client._set_state(STATE_DISCONNECTED)

    assert seen == []


def test_update_audio_volumes_sends_audio_payload():
    client = EngineClient("unused.sock")
    calls = []
    audio = {"tracks": [{"track": 1, "volume": 0.5}]}

    def send_command(cmd, callback=None, **params):
        calls.append((cmd, callback, params))
        return True

    client.send_command = send_command

    assert client.update_audio_volumes(audio, None)
    assert calls == [("update_audio_volumes", None, {"audio": audio})]


def test_save_replay_buffer_sends_optional_game_name():
    client = EngineClient("unused.sock")
    calls = []

    def send_command(cmd, callback=None, **params):
        calls.append((cmd, callback, params))
        return True

    client.send_command = send_command

    assert client.save_replay_buffer(None, game_name="Counter-Strike 2")
    assert calls[0][0] == "save_replay_buffer"
    assert calls[0][2] == {"game_name": "Counter-Strike 2"}
    assert client.pending_saves == 1
    calls[0][1]({"ok": True})
    assert client.pending_saves == 1
    assert not client.save_replay_buffer()
    client._process_message('{"event": "clip_saved", "path": "/tmp/clip.mkv"}')
    assert client.pending_saves == 0


def test_save_replay_buffer_omits_empty_game_name():
    client = EngineClient("unused.sock")
    calls = []

    def send_command(cmd, callback=None, **params):
        calls.append((cmd, callback, params))
        return True

    client.send_command = send_command

    assert client.save_replay_buffer(None, game_name=None)
    assert calls[0][0] == "save_replay_buffer"
    assert calls[0][2] == {}
    assert client.pending_saves == 1
    calls[0][1]({"ok": True})
    assert client.pending_saves == 1
    client._process_message('{"event": "clip_saved", "path": "/tmp/clip.mkv"}')
    assert client.pending_saves == 0


def test_save_counter_recovers_from_failed_send():
    client = EngineClient("unused.sock")
    client.send_command = lambda *_args, **_kwargs: False
    assert not client.save_replay_buffer()
    assert client.pending_saves == 0


@pytest.mark.parametrize("event", ["clip_saved", "clip_save_failed"])
@pytest.mark.parametrize("event_first", [False, True])
def test_save_callback_waits_for_completion_and_allows_retry(event, event_first):
    client = EngineClient("unused.sock")
    acknowledgements = []

    def send(_command, callback, **_params):
        acknowledgements.append(callback)
        return True

    client.send_command = send
    results = []
    assert client.save_replay_buffer(results.append)
    if not event_first:
        acknowledgements[0]({"ok": True})
        assert not results
        assert client.pending_saves == 1
    client._process_message('{"event": "' + event + '", "path": "/tmp/clip.mkv"}')
    assert client.pending_saves == 0
    assert results == [
        {"ok": True, "path": "/tmp/clip.mkv"}
        if event == "clip_saved" else {"ok": False, "error": "save_failed"}
    ]
    assert client.save_replay_buffer(results.append)
    if event_first:
        acknowledgements[0]({"ok": True})
    assert len(results) == 1
    assert client.pending_saves == 1


def test_rejected_save_finishes_once_without_waiting_for_event():
    client = EngineClient("unused.sock")
    client.send_command = lambda _cmd, callback, **_params: (callback({"ok": False}) or True)
    results = []
    assert client.save_replay_buffer(results.append)
    assert results == [{"ok": False}]
    assert client.pending_saves == 0


def test_get_preview_frame_sends_size():
    client = EngineClient("unused.sock")
    calls = []

    def send_command(cmd, callback=None, **params):
        calls.append((cmd, callback, params))
        return True

    def callback(_response):
        pass

    client.send_command = send_command

    assert client.get_preview_frame(callback, width=320, height=180)
    assert calls == [
        ("get_preview_frame", callback, {"width": 320, "height": 180})
    ]


def test_get_preview_frame_can_request_aspect_preserved_bounds():
    client = EngineClient("unused.sock")
    calls = []

    def send_command(cmd, callback=None, **params):
        calls.append((cmd, callback, params))
        return True

    def callback(_response):
        pass

    client.send_command = send_command

    assert client.get_preview_frame(
        callback, width=360, height=203, preserve_aspect=True
    )
    assert calls == [
        (
            "get_preview_frame",
            callback,
            {"width": 360, "height": 203, "preserve_aspect": True},
        )
    ]
