"""Application-owned recording capability discovery shared by all windows."""

from collections.abc import Callable
from typing import Any

_POLL_MS = 250
_POLL_LIMIT = 40
_RESPONSE_RETRY_LIMIT = 3


def has_encoder_capabilities(response: dict) -> bool:
    """Reject early status replies and incomplete encoder discovery."""
    return bool(response.get("ok")) and all(
        isinstance(response.get(key), list)
        and any(isinstance(enc, dict) and enc.get("id") for enc in response[key])
        for key in ("video_encoders", "audio_encoders")
    )


class RecordingCapabilityProbe:
    """Share one bounded engine request and cache across startup and screens.

    All methods and callbacks run on the application's GLib main context.
    Windows may unsubscribe without cancelling the application's warmup.
    """

    def __init__(
        self,
        engine_client: Any,
        scheduler: Any,
        acquire_engine: Callable[[], bool],
        release_engine: Callable[[], None],
        cache: dict,
    ) -> None:
        self._client = engine_client
        self._scheduler = scheduler
        self._acquire_engine = acquire_engine
        self._release_engine = release_engine
        self._cache = cache
        self._callbacks: list[Callable[[dict | None], None]] = []
        self._active = False
        self._closed = False
        self._starting = False
        self._engine_acquired = False
        self._in_flight = False
        self._generation = 0
        self._poll_id: int | None = None
        self._poll_count = 0
        self._response_retries = 0
        self._client.on_state_change(self._on_state_change)

    def request(self, callback: Callable[[dict | None], None] | None = None) -> None:
        if self._closed:
            return
        if has_encoder_capabilities(self._cache):
            if callback is not None:
                callback(self._cache)
            return
        if callback is not None and callback not in self._callbacks:
            self._callbacks.append(callback)
        if self._active:
            return

        self._active = True
        self._generation += 1
        self._poll_count = 0
        self._response_retries = 0
        self._poll_id = self._scheduler.timeout_add(_POLL_MS, self._poll)
        # Acquiring can synchronously connect IPC. Wait until ownership is
        # established before accepting a reply that might release the engine.
        self._starting = True
        self._engine_acquired = self._acquire_engine()
        self._starting = False
        if not self._engine_acquired:
            self._complete(None)
        else:
            self._fetch()

    def unsubscribe(self, callback: Callable[[dict | None], None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def close(self) -> None:
        self._closed = True
        self._callbacks.clear()
        self._complete(None)
        self._client.remove_state_change_callback(self._on_state_change)

    def _poll(self) -> bool:
        if not self._active:
            return False
        self._poll_count += 1
        if self._poll_count >= _POLL_LIMIT:
            self._complete(None)
        else:
            self._fetch()
        return self._active

    def _on_state_change(self, _state: int) -> None:
        if not self._active or self._starting:
            return
        if not self._client.is_connected():
            # A reconnect drops the old IPC queue. Its late responses must not
            # complete a newer request or overwrite its cache.
            self._generation += 1
            self._in_flight = False
        else:
            self._fetch()

    def _fetch(self) -> None:
        if not self._active or self._in_flight or not self._client.is_connected():
            return
        self._in_flight = True
        generation = self._generation
        if not self._client.get_capabilities(
            lambda response: self._on_response(response, generation)
        ):
            self._complete(None)

    def _on_response(self, response: dict, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        self._in_flight = False
        if not response.get("ok"):
            self._complete(None)
        elif not has_encoder_capabilities(response):
            self._response_retries += 1
            if self._response_retries > _RESPONSE_RETRY_LIMIT:
                self._complete(None)
        else:
            self._cache.clear()
            self._cache.update(response)
            self._complete(self._cache)

    def _complete(self, response: dict | None) -> None:
        self._active = False
        self._in_flight = False
        self._generation += 1
        if self._poll_id is not None:
            self._scheduler.source_remove(self._poll_id)
            self._poll_id = None
        callbacks, self._callbacks = self._callbacks, []
        if self._engine_acquired:
            self._engine_acquired = False
            self._release_engine()
        for callback in callbacks:
            callback(response)
