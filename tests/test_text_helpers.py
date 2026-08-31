from text_helpers import (
    ELLIPSIZE_MIDDLE,
    configure_single_line_ellipsis,
    escape_markup_text,
    middle_truncate_text,
    set_single_line_label_text,
)


class LabelStub:
    def __init__(self, text=""):
        self.text = text
        self.tooltip_text = None
        self.hexpand = False
        self.width_chars = None
        self.ellipsize = None
        self.single_line_mode = False
        self.max_width_chars = None

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text

    def set_tooltip_text(self, tooltip_text):
        self.tooltip_text = tooltip_text

    def set_hexpand(self, hexpand):
        self.hexpand = bool(hexpand)

    def set_width_chars(self, width_chars):
        self.width_chars = width_chars

    def set_ellipsize(self, ellipsize):
        self.ellipsize = ellipsize

    def set_single_line_mode(self, single_line_mode):
        self.single_line_mode = bool(single_line_mode)

    def set_max_width_chars(self, max_width_chars):
        self.max_width_chars = max_width_chars


def test_configure_single_line_ellipsis_allows_label_to_shrink():
    label = LabelStub("/very/long/path/to/game.exe")

    configure_single_line_ellipsis(label, max_width_chars=40)

    assert label.hexpand is True
    assert label.width_chars == 1
    assert label.ellipsize == ELLIPSIZE_MIDDLE
    assert label.single_line_mode is True
    assert label.max_width_chars == 40
    assert label.tooltip_text == "/very/long/path/to/game.exe"


def test_set_single_line_label_text_keeps_full_tooltip():
    label = LabelStub()

    set_single_line_label_text(label, "App: Browser - long window title")

    assert label.text == "App: Browser - long window title"
    assert label.tooltip_text == "App: Browser - long window title"


def test_middle_truncate_text_bounds_toolkit_owned_labels():
    text = "/run/media/user/very/deep/library/path/with/a/game/executable.exe"

    truncated = middle_truncate_text(text, 24)

    assert truncated == "/run/media...cutable.exe"
    assert len(truncated) == 24


def test_escape_markup_text_keeps_toast_titles_plain_text():
    assert (
        escape_markup_text("Added Bits & Bops <Demo>.exe")
        == "Added Bits &amp; Bops &lt;Demo&gt;.exe"
    )
