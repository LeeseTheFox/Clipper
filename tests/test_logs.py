import logging
import os
import re

from logs import (
    ApplicationLogHandler,
    LogBuffer,
    consume_handoff,
    create_handoff,
    diagnostic_tail,
    probe_failure_reason,
)


def test_probe_reason_preserves_driver_cause_before_cleanup_noise():
    detail = (
        "[h264_nvenc @ 0x123abc] Cannot load libcuda.so.1\n"
        "[vost#0:0 @ 0xabc] Error while opening encoder\n"
        "Task finished with error code: -22 (Invalid argument)\n"
        "Terminating thread with return code -22\n"
        "Nothing was written into output file\n"
    )
    assert probe_failure_reason(detail) == "[h264_nvenc] Cannot load libcuda.so.1"
    assert probe_failure_reason("Nothing was written into output file") == (
        "no specific driver reason reported"
    )


def test_repeated_messages_keep_count_and_do_not_hide_later_recurrence():
    logs = LogBuffer()
    logs.add("[audio] disconnected")
    logs.add("[audio] disconnected")
    logs.add("[audio] disconnected")
    assert len(logs.snapshot()) == 1
    assert "repeated 3 times" in logs.text()
    logs.add("[audio] recovered")
    logs.add("[audio] disconnected")
    assert len(logs.snapshot()) == 3
    assert logs.snapshot()[-1].endswith("[audio] disconnected")


def test_component_diagnostics_reach_application_log():
    logs = LogBuffer()
    handler = ApplicationLogHandler(logs.add)
    record = logging.LogRecord("clipper.editor.export", logging.ERROR, "", 0,
                               "Encoder %s failed", ("vaapi",), None)
    handler.handle(record)
    assert "[clipper.editor.export] ERROR: Encoder vaapi failed" in logs.text()


def test_diagnostic_tail_bounds_and_collapses_external_output():
    assert diagnostic_tail("first\nfirst\nsecond\nthird", 2) == (
        "[earlier diagnostic lines omitted]\nsecond\nthird"
    )
    assert len(diagnostic_tail("x" * 2000)) == 1000


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
