import pytest
from editor_model import MAX_GAIN, AudioTrack, EditorProject, Source


def source(track_count=2):
    return Source(
        path="/clip.mkv",
        size=10,
        mtime_ns=20,
        duration_us=10_000_000,
        video_stream_index=0,
        audio_tracks=[AudioTrack(f"a{i}", i, i + 1, f"Track {i + 1}") for i in range(track_count)],
        frame_rate_num=25,
    )


def test_split_delete_and_independent_audio_settings():
    project = EditorProject.new(source())
    original = project.segments[0].id
    left, right = project.split(original, 3_000_000)
    middle, right = project.split(right, 7_000_000)
    project.set_gain(left, "a0", 0.5)
    project.set_muted(middle, "a0", True)
    project.set_gain(right, "a0", 4)
    assert [segment.audio["a0"].gain for segment in project.segments] == [0.5, 0, 4]
    assert project.segments[1].audio["a0"].muted is True
    assert all(segment.audio["a1"].gain == 1 for segment in project.segments)
    project.delete(middle)
    assert project.output_duration_us == 6_000_000
    assert project.output_to_source_us(3_500_000) == 7_500_000


def test_serialization_round_trip_and_boundaries():
    project = EditorProject.new(source(6))
    restored = EditorProject.from_dict(project.to_dict())
    assert restored.digest() == project.digest()
    with pytest.raises(ValueError):
        project.split(project.segments[0].id, 1)
    with pytest.raises(ValueError):
        project.delete(project.segments[0].id)
    project.set_gain(project.segments[0].id, "a0", MAX_GAIN)
    assert project.segments[0].audio["a0"].gain == pytest.approx(31.6227766)
    with pytest.raises(ValueError):
        project.set_gain(project.segments[0].id, "a0", MAX_GAIN + 0.1)


def test_zero_gain_and_mute_stay_in_sync():
    project = EditorProject.new(source(1))
    segment = project.segments[0]

    project.set_gain(segment.id, "a0", 0)
    assert segment.audio["a0"].gain == 0
    assert segment.audio["a0"].muted is True

    project.set_gain(segment.id, "a0", 0.4)
    assert segment.audio["a0"].gain == 0.4
    assert segment.audio["a0"].muted is False

    project.set_muted(segment.id, "a0", True)
    assert segment.audio["a0"].gain == 0
    assert segment.audio["a0"].muted is True

    project.set_muted(segment.id, "a0", False)
    assert segment.audio["a0"].gain == 1
    assert segment.audio["a0"].muted is False


def test_delete_many_removes_disjoint_segments_and_keeps_the_nearest_survivor():
    project = EditorProject.new(source(1))
    _first, remainder = project.split(project.segments[0].id, 2_000_000)
    _second, remainder = project.split(remainder, 4_000_000)
    _third, remainder = project.split(remainder, 6_000_000)
    project.split(remainder, 8_000_000)
    original_ids = [segment.id for segment in project.segments]

    selected_after_delete = project.delete_many((original_ids[1], original_ids[3]))

    assert [segment.id for segment in project.segments] == [
        original_ids[0],
        original_ids[2],
        original_ids[4],
    ]
    assert selected_after_delete == original_ids[2]
    assert project.output_duration_us == 6_000_000


def test_delete_many_rejects_deleting_every_segment_without_changing_project():
    project = EditorProject.new(source(0))
    project.split(project.segments[0].id, 5_000_000)
    original_digest = project.digest()

    with pytest.raises(ValueError, match="final segment"):
        project.delete_many(segment.id for segment in project.segments)

    assert project.digest() == original_digest
