import wave
from pathlib import Path

import pytest
from sound_feedback import CLIP_SAVED_SOUND_PATH, ERROR_SOUND_PATH, SoundPlayer


class StreamStub:
    def __init__(self):
        self.play_calls = 0
        self.volumes = []

    def set_volume(self, volume):
        self.volumes.append(volume)

    def play(self):
        self.play_calls += 1


@pytest.mark.parametrize("sound_path", [CLIP_SAVED_SOUND_PATH, ERROR_SOUND_PATH])
def test_bundled_feedback_sound_contains_decodable_pcm_frames(sound_path):
    with wave.open(str(sound_path)) as sound:
        assert sound.getcomptype() == "NONE"
        assert sound.getnframes() > 0
        assert len(sound.readframes(sound.getnframes())) == (
            sound.getnframes() * sound.getnchannels() * sound.getsampwidth()
        )


def test_sound_player_plays_at_unity_gain_and_retains_media_stream(tmp_path):
    sound_path = tmp_path / "feedback.wav"
    sound_path.write_bytes(b"RIFF")
    stream = StreamStub()
    requested_paths = []
    player = SoundPlayer(
        sound_path,
        lambda path: requested_paths.append(path) or stream,
    )

    assert player.play() is True
    assert requested_paths == [str(sound_path)]
    assert stream.play_calls == 1
    assert stream.volumes == [1.0]
    assert player._stream is stream


def test_sound_player_fails_safely_when_media_cannot_be_created(tmp_path):
    sound_path = tmp_path / "feedback.wav"
    sound_path.write_bytes(b"RIFF")
    player = SoundPlayer(
        sound_path,
        lambda _path: (_ for _ in ()).throw(RuntimeError("no audio backend")),
    )

    assert player.play() is False


def test_sound_player_does_not_create_media_for_a_missing_file(tmp_path):
    requested_paths = []
    player = SoundPlayer(
        Path(tmp_path / "missing.wav"),
        lambda path: requested_paths.append(path),
    )

    assert player.play() is False
    assert requested_paths == []
