from pathlib import Path

from sound_feedback import CLIP_SAVED_SOUND_PATH, ClipSoundPlayer


class StreamStub:
    def __init__(self):
        self.play_calls = 0
        self.volumes = []

    def set_volume(self, volume):
        self.volumes.append(volume)

    def play(self):
        self.play_calls += 1


def test_bundled_clip_saved_sound_exists_and_is_a_wave_file():
    assert CLIP_SAVED_SOUND_PATH.is_file()
    assert CLIP_SAVED_SOUND_PATH.read_bytes()[:4] == b"RIFF"


def test_sound_player_plays_and_retains_media_stream(tmp_path):
    sound_path = tmp_path / "feedback.wav"
    sound_path.write_bytes(b"RIFF")
    stream = StreamStub()
    requested_paths = []
    player = ClipSoundPlayer(
        sound_path,
        lambda path: requested_paths.append(path) or stream,
    )

    assert player.play(1.75) is True
    assert requested_paths == [str(sound_path)]
    assert stream.play_calls == 1
    assert stream.volumes == [1.75]
    assert player._stream is stream


def test_sound_player_fails_safely_when_media_cannot_be_created(tmp_path):
    sound_path = tmp_path / "feedback.wav"
    sound_path.write_bytes(b"RIFF")
    player = ClipSoundPlayer(
        sound_path,
        lambda _path: (_ for _ in ()).throw(RuntimeError("no audio backend")),
    )

    assert player.play() is False


def test_sound_player_does_not_create_media_for_a_missing_file(tmp_path):
    requested_paths = []
    player = ClipSoundPlayer(
        Path(tmp_path / "missing.wav"),
        lambda path: requested_paths.append(path),
    )

    assert player.play() is False
    assert requested_paths == []


def test_sound_player_clamps_volume_to_supported_range(tmp_path):
    sound_path = tmp_path / "feedback.wav"
    sound_path.write_bytes(b"RIFF")
    stream = StreamStub()
    player = ClipSoundPlayer(sound_path, lambda _path: stream)

    assert player.play(3) is True

    assert stream.volumes == [2.0]
