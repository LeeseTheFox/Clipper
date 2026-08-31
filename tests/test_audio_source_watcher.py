import pytest

pytest.importorskip("gi")

from audio_source_watcher import audio_event_is_relevant


@pytest.mark.parametrize(
    "line",
    [
        "Event 'new' on sink-input #119",
        "Event 'remove' on client #92",
        "Event 'change' on source #1",
        "Event 'change' on server #0",
    ],
)
def test_audio_event_is_relevant_for_discovery_subjects(line):
    assert audio_event_is_relevant(line)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Event 'change' on card #48",
        "Event 'new' on module #3",
        "unrelated output",
    ],
)
def test_audio_event_ignores_irrelevant_lines(line):
    assert not audio_event_is_relevant(line)
