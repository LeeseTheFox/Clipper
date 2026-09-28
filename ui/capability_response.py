"""Shared response handling for runtime encoder discovery.

Screens retain ownership of starting/stopping the engine and presenting options.
The mixin owns validation, bounded retries and cancellation of late responses.
"""

from typing import Any

_CAPABILITY_RESPONSE_RETRY_LIMIT = 3
_CAPABILITY_RESPONSE_RETRY_DELAY_MS = 250


class CapabilityResponseHandler:
    _capability_response_cancelled = False
    _capability_response_retry_id: Any = None
    _capability_response_retry_count = 0
    _capability_request_in_flight = False
    _capabilities_cache: Any = None

    def _cancel_capability_response(self):
        self._capability_response_cancelled = True
        if self._capability_response_retry_id is not None:
            self._capability_scheduler.source_remove(self._capability_response_retry_id)
            self._capability_response_retry_id = None

    _engine_client: Any = None
    _capability_scheduler: Any = None

    def _restore_fallback_capability_options(self):
        raise NotImplementedError

    def _finish_capability_probe(self):
        raise NotImplementedError

    def _apply_capabilities(self, response):
        raise NotImplementedError

    def _capability_response_has_encoders(self, response):
        """Return True only for a real capabilities payload, not any ok response."""
        for key in ("video_encoders", "audio_encoders"):
            encoders = response.get(key)
            if not isinstance(encoders, list):
                return False
            if not any(isinstance(enc, dict) and enc.get("id") for enc in encoders):
                return False
        return True

    def _retry_capability_response(self):
        self._capability_response_retry_id = None
        if self._capability_response_cancelled:
            return False
        if not self._engine_client or not self._engine_client.is_connected():
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return False

        self._capability_request_in_flight = True
        if not self._engine_client.get_capabilities(self._on_capabilities):
            self._capability_request_in_flight = False
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
        return False

    def _on_capabilities(self, response):
        """Update OBS-dependent dropdowns from the engine capability response."""
        if self._capability_response_cancelled:
            return
        self._capability_request_in_flight = False
        if not response.get("ok"):
            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return

        if not self._capability_response_has_encoders(response):
            if (
                self._capability_response_retry_count
                < _CAPABILITY_RESPONSE_RETRY_LIMIT
            ):
                self._capability_response_retry_count += 1
                self._capability_response_retry_id = self._capability_scheduler.timeout_add(
                    _CAPABILITY_RESPONSE_RETRY_DELAY_MS,
                    self._retry_capability_response,
                )
                return

            self._restore_fallback_capability_options()
            self._finish_capability_probe()
            return

        self._capability_response_retry_count = 0
        if self._capabilities_cache is not None:
            self._capabilities_cache.clear()
            self._capabilities_cache.update(response)
        self._finish_capability_probe()
        self._apply_capabilities(response)
