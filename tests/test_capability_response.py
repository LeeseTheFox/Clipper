"""Shared recording discovery must own one engine request and reject stale work."""

from types import SimpleNamespace

import pytest
from capability_response import RecordingCapabilityProbe


class Scheduler:
    def __init__(self):
        self.sources = {}
        self.next_id = 0

    def timeout_add(self, _delay, callback):
        self.next_id += 1
        self.sources[self.next_id] = callback
        return self.next_id

    def source_remove(self, source_id):
        self.sources.pop(source_id, None)

    def tick(self):
        for source_id, callback in list(self.sources.items()):
            if not callback():
                self.sources.pop(source_id, None)


@pytest.fixture
def discovery():
    scheduler = Scheduler()
    pending = []
    state_callbacks = []
    ownership = []
    response = {
        "ok": True,
        "video_encoders": [{"id": "obs_x264"}],
        "audio_encoders": [{"id": "ffmpeg_aac"}],
    }
    client = SimpleNamespace(
        connected=False,
        is_connected=lambda: client.connected,
        on_state_change=state_callbacks.append,
        remove_state_change_callback=state_callbacks.remove,
        get_capabilities=lambda callback: pending.append(callback) or True,
    )

    def set_connected(connected):
        client.connected = connected
        for callback in list(state_callbacks):
            callback(2 if connected else 0)

    cache = {}
    probe = RecordingCapabilityProbe(
        client, scheduler,
        lambda: ownership.append("acquire") or True,
        lambda: ownership.append("release"), cache,
    )
    return SimpleNamespace(
        probe=probe, client=client, scheduler=scheduler, cache=cache,
        pending=pending, ownership=ownership, response=response,
        set_connected=set_connected, state_callbacks=state_callbacks,
    )


def test_startup_and_windows_share_one_request_and_cached_result(discovery):
    d = discovery
    first, second = [], []
    d.probe.request()
    d.probe.request(first.append)
    d.probe.request(second.append)
    d.set_connected(True)
    assert len(d.pending) == 1
    d.scheduler.tick()
    assert len(d.pending) == 1
    d.pending.pop()(d.response)
    assert first == second == [d.response]
    assert d.ownership == ["acquire", "release"]
    assert not d.scheduler.sources
    d.set_connected(False)
    d.probe.request(first.append)
    assert first == [d.response, d.response]
    assert d.pending == []
    assert d.ownership == ["acquire", "release"]


def test_synchronous_connect_and_reply_release_engine_after_acquisition(discovery):
    d = discovery
    events = []

    def acquire():
        d.set_connected(True)
        events.append("acquired")
        return True

    probe = RecordingCapabilityProbe(
        d.client, d.scheduler, acquire, lambda: events.append("released"), d.cache
    )
    d.client.get_capabilities = lambda callback: callback(d.response) or True
    results = []
    probe.request(results.append)
    assert events == ["acquired", "released"]
    assert results == [d.response]
    assert not d.scheduler.sources


@pytest.mark.parametrize("connected", [False, True], ids=["connection", "response"])
def test_timeout_releases_engine_and_late_reply_cannot_complete_new_probe(discovery, connected):
    d = discovery
    d.set_connected(connected)
    first, second = [], []
    d.probe.request(first.append)
    for _ in range(40):
        d.scheduler.tick()
    assert first == [None]
    assert d.ownership == ["acquire", "release"]
    assert not d.scheduler.sources
    old_reply = d.pending.pop() if d.pending else None
    d.set_connected(True)
    d.probe.request(second.append)
    if old_reply:
        old_reply(d.response)
        assert second == []
        assert not d.cache
    d.pending.pop()(d.response)
    assert second == [d.response]
    assert d.ownership == ["acquire", "release", "acquire", "release"]


def test_reconnect_reissues_lost_request_and_rejects_old_reply(discovery):
    d = discovery
    d.set_connected(True)
    results = []
    d.probe.request(results.append)
    old_reply = d.pending.pop()
    d.set_connected(False)
    d.set_connected(True)
    assert len(d.pending) == 1
    old_reply(d.response)
    assert not d.cache
    assert results == []
    d.pending.pop()(d.response)
    assert results == [d.response]
    assert d.ownership == ["acquire", "release"]


@pytest.mark.parametrize("recover", [True, False], ids=["ready-on-retry", "retry-limit"])
def test_incomplete_encoder_responses_retry_without_caching_partial_results(discovery, recover):
    d = discovery
    d.set_connected(True)
    results = []
    d.probe.request(results.append)
    for _ in range(3):
        d.pending.pop()({"ok": True, "video_encoders": []})
        assert not d.cache
        assert results == []
        d.scheduler.tick()
    d.pending.pop()(d.response if recover else {"ok": True})
    assert results == [d.response if recover else None]
    assert d.ownership == ["acquire", "release"]
    assert not d.scheduler.sources


@pytest.mark.parametrize("failure", ["start", "send", "response", "close"])
def test_failure_or_shutdown_releases_only_acquired_engine_and_never_caches(discovery, failure):
    d = discovery
    d.set_connected(True)
    results = []
    if failure == "start":
        d.probe._acquire_engine = lambda: False
    elif failure == "send":
        d.client.get_capabilities = lambda callback: False
    d.probe.request(results.append)
    if failure == "response":
        d.pending.pop()({"ok": False})
    elif failure == "close":
        d.probe.close()
        d.pending.pop()(d.response)
        d.probe.request(results.append)
        assert not d.state_callbacks
    assert results == ([] if failure == "close" else [None])
    assert d.ownership == ([] if failure == "start" else ["acquire", "release"])
    assert not d.cache
    assert not d.scheduler.sources
