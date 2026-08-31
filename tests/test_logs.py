import os
import re

from logs import LogBuffer, consume_handoff, create_handoff


def test_log_buffer_notifies_listeners_and_keeps_recent_lines():
    logs = LogBuffer(max_lines=2)
    changes = []
    unsubscribe = logs.subscribe(lambda: changes.append(True))

    logs.add("first\nsecond")
    logs.add("third")
    unsubscribe()
    logs.add("fourth")

    # Each line should have format [HH:MM:SS] message
    text = logs.text()
    lines = text.split("\n")
    assert len(lines) == 2
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] third$", lines[0])
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] fourth$", lines[1])
    assert changes == [True, True]


def test_log_buffer_supports_empty_messages():
    logs = LogBuffer()

    logs.add("")

    # Empty message gets a timestamp prefix
    text = logs.text()
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] $", text)


def test_log_buffer_handoff_preserves_bounded_history_across_exec():
    logs = LogBuffer(max_lines=2)
    logs.add("discarded\nkept")
    logs.add("newest")

    handoff = create_handoff(logs)
    try:
        assert os.get_inheritable(handoff.fileno()) is True
        inherited_descriptor = os.dup(handoff.fileno())

        restored = consume_handoff(inherited_descriptor)

        # Each line should have format [HH:MM:SS] message
        text = restored.text()
        lines = text.split("\n")
        assert len(lines) == 2
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] kept$", lines[0])
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] newest$", lines[1])
    finally:
        handoff.close()
