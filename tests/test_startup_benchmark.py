import tools.benchmark_flatpak_startup as benchmark
from tools.benchmark_flatpak_startup import Result, StatusItem


def test_result_clip_delay_cannot_be_negative():
    assert Result(window_ms=500, clips_ms=450).clips_after_window_ms == 0


def test_result_clip_delay_reports_time_after_window():
    assert Result(window_ms=250, clips_ms=575).clips_after_window_ms == 325


def test_engine_detection_requires_the_actual_engine_process(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "process_rows",
        lambda: [("bash", "rg clipper-engine")],
    )
    assert not benchmark.engine_is_running()

    monkeypatch.setattr(
        benchmark,
        "process_rows",
        lambda: [("clipper-engine", "/app/libexec/clipper/clipper-engine")],
    )
    assert benchmark.engine_is_running()


def test_background_handoff_detection_requires_python_main_process(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "process_rows",
        lambda: [("bash", "python3 /app/share/clipper/ui/main.py --background")],
    )
    assert not benchmark.background_handoff_is_running()

    monkeypatch.setattr(
        benchmark,
        "process_rows",
        lambda: [
            ("python3", "/usr/bin/python3 /app/share/clipper/ui/main.py --background")
        ],
    )
    assert benchmark.background_handoff_is_running()


def test_registered_status_items_parse_unique_service_and_path():
    session = object.__new__(benchmark.DesktopSession)
    session._call = lambda *_args, **_kwargs: ReplyStub(
        ([":1.25/StatusNotifierItem", "org.example.Other/Menu"],)
    )

    assert session.registered_status_items() == [
        StatusItem(":1.25", "/StatusNotifierItem"),
        StatusItem("org.example.Other", "/Menu"),
    ]


def test_presentation_state_decodes_action_state():
    session = object.__new__(benchmark.DesktopSession)
    session._call = lambda *_args, **_kwargs: ReplyStub(
        ((False, "", ['{"pid":5,"window":123,"clips":456}']),)
    )

    assert session.presentation_state(":1.25") == {
        "pid": 5,
        "window": 123,
        "clips": 456,
    }


class ReplyStub:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value
