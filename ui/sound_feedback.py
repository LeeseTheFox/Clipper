"""Short, optional audio feedback for successful clip captures."""

from collections.abc import Callable
from pathlib import Path

CLIP_SAVED_SOUND_PATH = Path(__file__).resolve().parent / "sounds" / "clip-saved.wav"


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

    def play(self) -> bool:
        """Start playback and return whether the sound could be started."""
        if not self._sound_path.is_file():
            return False

        try:
            media_factory = self._media_factory
            if media_factory is None:
                from gi.repository import Gtk

                media_factory = Gtk.MediaFile.new_for_filename
            self._stream = media_factory(str(self._sound_path))
            self._stream.play()
        except Exception:  # Audio feedback must never turn a saved clip into an error.
            self._stream = None
            return False

        return True
