"""Short, optional audio feedback for successful clip captures."""

from collections.abc import Callable
from pathlib import Path

from config import normalize_clip_sound_volume

CLIP_SAVED_SOUND_PATH = Path(__file__).resolve().parent / "sounds" / "clip-saved.wav"


class _GstSoundStream:
    """Small GStreamer-backed stream that permits gain above 100%."""

    def __init__(self, filename: str) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._gst = Gst
        self._pipeline = Gst.ElementFactory.make(
            "playbin3", "clipper-capture-feedback"
        ) or Gst.ElementFactory.make("playbin", "clipper-capture-feedback")
        if self._pipeline is None:
            raise RuntimeError("GStreamer playback is unavailable")
        self._pipeline.set_property("uri", Path(filename).resolve().as_uri())

    def set_volume(self, volume: float) -> None:
        self._pipeline.set_property("volume", volume)

    def play(self) -> None:
        result = self._pipeline.set_state(self._gst.State.PLAYING)
        if result == self._gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(self._gst.State.NULL)
            raise RuntimeError("GStreamer could not start playback")

    def stop(self) -> None:
        self._pipeline.set_state(self._gst.State.NULL)


class ClipSoundPlayer:
    """Play the bundled clip-success sound while retaining the media stream."""

    def __init__(
        self,
        sound_path: Path = CLIP_SAVED_SOUND_PATH,
        media_factory: Callable[[str], object] | None = None,
    ) -> None:
        self._sound_path = Path(sound_path)
        self._media_factory = media_factory
        self._stream = None

    def play(self, volume: float = 1.0) -> bool:
        """Start playback and return whether the sound could be started."""
        if not self._sound_path.is_file():
            return False

        try:
            previous_stream = self._stream
            if previous_stream is not None and hasattr(previous_stream, "stop"):
                previous_stream.stop()
            media_factory = self._media_factory
            if media_factory is None:
                media_factory = _GstSoundStream
            self._stream = media_factory(str(self._sound_path))
            self._stream.set_volume(normalize_clip_sound_volume(volume))
            self._stream.play()
        except Exception:  # Audio feedback must never turn a saved clip into an error.
            self._stream = None
            return False

        return True
