import importlib
import sys
import types


def _load_focus_helpers():
    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "focus_helpers",
            "gi",
            "gi.repository",
            "text_helpers",
        )
    }

    idle_callbacks = []

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    repository.GLib = types.SimpleNamespace(
        idle_add=lambda callback, *args: idle_callbacks.append((callback, args))
    )
    repository.Gtk = types.SimpleNamespace(
        DropDown=DropDownStub,
        EventControllerScroll=types.SimpleNamespace(
            new=lambda flags: ScrollControllerStub(flags)
        ),
        EventControllerScrollFlags=types.SimpleNamespace(VERTICAL=1, HORIZONTAL=2),
        PropagationPhase=types.SimpleNamespace(CAPTURE=3),
        SignalListItemFactory=SignalListItemFactoryStub,
        Widget=type("Widget", (), {}),
    )
    gi.repository = repository

    text_helpers = types.ModuleType("text_helpers")
    text_helpers.ELLIPSIZE_END = "end"
    text_helpers.configure_single_line_ellipsis = lambda *_args, **_kwargs: None
    text_helpers.set_single_line_label_text = lambda *_args, **_kwargs: None

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "text_helpers": text_helpers,
        }
    )
    sys.modules.pop("focus_helpers", None)

    try:
        module = importlib.import_module("focus_helpers")
        module._idle_callbacks = idle_callbacks
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class RootStub:
    def __init__(self):
        self.focus_values = []
        self.presented = False

    def set_focus(self, value):
        self.focus_values.append(value)

    def present(self):
        self.presented = True


class DropDownStub:
    def __init__(self, labels):
        self.labels = labels
        self.selected = 0
        self.focus_on_click = True
        self.connections = []
        self.controllers = []
        self.root = None
        self.size_request = None
        self.factory = None
        self.list_factory = None

    @classmethod
    def new_from_strings(cls, labels):
        return cls(labels)

    def set_focus_on_click(self, value):
        self.focus_on_click = value

    def set_size_request(self, width, height):
        self.size_request = (width, height)

    def set_factory(self, factory):
        self.factory = factory

    def set_list_factory(self, factory):
        self.list_factory = factory

    def set_selected(self, selected):
        self.selected = selected

    def get_selected(self):
        return self.selected

    def connect(self, signal_name, callback):
        self.connections.append((signal_name, callback))

    def add_controller(self, controller):
        self.controllers.append(controller)

    def get_root(self):
        return self.root


class ScrollControllerStub:
    def __init__(self, flags):
        self.flags = flags
        self.phase = None
        self.connections = []

    def set_propagation_phase(self, phase):
        self.phase = phase

    def connect(self, signal_name, callback):
        self.connections.append((signal_name, callback))


class SignalListItemFactoryStub:
    def __init__(self):
        self.connections = []

    def connect(self, signal_name, callback):
        self.connections.append((signal_name, callback))


def test_new_id_dropdown_stores_ids_and_consumes_widget_scroll():
    focus_helpers = _load_focus_helpers()

    dropdown = focus_helpers.new_id_dropdown(
        (("Newest", "newest"), ("Oldest", "oldest")),
        "oldest",
    )

    assert dropdown.labels == ["Newest", "Oldest"]
    assert dropdown._clipper_option_ids == ["newest", "oldest"]
    assert dropdown.selected == 1
    assert dropdown.focus_on_click is False
    assert focus_helpers.dropdown_active_id(dropdown, "newest") == "oldest"

    assert len(dropdown.controllers) == 1
    controller = dropdown.controllers[0]
    assert controller.flags == 3
    assert controller.phase == 3
    signal_name, callback = controller.connections[0]
    assert signal_name == "scroll"
    assert callback(controller, 0, 1) is True


def test_new_id_dropdown_clears_and_presents_window_after_selection():
    focus_helpers = _load_focus_helpers()
    root = RootStub()
    dropdown = focus_helpers.new_id_dropdown((("Display", "display"),), "missing")
    dropdown.root = root

    signal_name, callback = dropdown.connections[0]
    assert signal_name == "notify::selected"
    callback(dropdown, None)

    assert root.focus_values == []
    assert len(focus_helpers._idle_callbacks) == 1

    idle_callback, args = focus_helpers._idle_callbacks[0]
    assert args == ()
    assert idle_callback() is False
    assert root.focus_values == [None]
    assert root.presented is True


def test_id_dropdown_can_limit_and_ellipsize_display_labels():
    focus_helpers = _load_focus_helpers()

    dropdown = focus_helpers.new_id_dropdown(
        (("Game capture", "game_capture"),),
        max_width_chars=18,
        width_request=180,
    )

    assert dropdown.size_request == (180, -1)
    assert dropdown.factory is not None
    assert dropdown.list_factory is not None
    assert [name for name, _callback in dropdown.factory.connections] == [
        "setup",
        "bind",
    ]


def test_dropdown_active_id_returns_default_for_invalid_selection():
    focus_helpers = _load_focus_helpers()
    dropdown = focus_helpers.new_id_dropdown((("Display", "display"),), "display")
    dropdown.selected = 999

    assert focus_helpers.dropdown_active_id(dropdown, "fallback") == "fallback"
