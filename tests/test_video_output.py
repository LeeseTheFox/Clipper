import video_output


class FakeCaps:
    def __init__(self, description):
        self.description = description

    def is_empty(self):
        return False

    def copy(self):
        return FakeCaps(self.description)


class FakeElement:
    def __init__(self, factory, name, *, link_succeeds=True):
        self.factory = factory
        self.name = name
        self.link_succeeds = link_succeeds
        self.parent = None
        self.properties = {}
        self.linked_to = []
        self.sink_pad = object()

    def set_property(self, name, value):
        self.properties[name] = value

    def get_name(self):
        return self.name

    def get_parent(self):
        return self.parent

    def get_static_pad(self, name):
        return self.sink_pad if name == "sink" else None

    def link(self, downstream):
        self.linked_to.append(downstream)
        return self.link_succeeds


class FakeBin(FakeElement):
    def __init__(self, name):
        super().__init__("bin", name)
        self.children = []
        self.pads = []
        self.states = []

    def add(self, element):
        self.children.append(element)
        element.parent = self
        return True

    def remove(self, element):
        self.children.remove(element)
        element.parent = None
        return True

    def add_pad(self, pad):
        self.pads.append(pad)
        return True

    def set_state(self, state):
        self.states.append(state)


def install_fake_gstreamer(monkeypatch, *, missing_factory=None, failed_link=None):
    elements = {}
    sink_bin = FakeBin("clipper-test")
    ghost_pads = []

    def make_element(factory, name):
        if factory == missing_factory:
            return None
        element = FakeElement(factory, name, link_succeeds=factory != failed_link)
        elements[factory, name] = element
        return element

    monkeypatch.setattr(video_output, "_make_element", make_element)
    monkeypatch.setattr(video_output, "_make_bin", lambda _name: sink_bin)
    monkeypatch.setattr(video_output, "_make_caps", FakeCaps)
    monkeypatch.setattr(
        video_output,
        "_make_ghost_pad",
        lambda name, target: ghost_pads.append((name, target)) or object(),
    )
    return sink_bin, elements, ghost_pads


def test_clipper_video_sink_builds_color_corrected_glmemory_bin(monkeypatch):
    sink_bin, elements, ghost_pads = install_fake_gstreamer(monkeypatch)

    result = video_output.make_clipper_video_sink("clipper-test")

    assert result.color_corrected is True
    assert result.element is sink_bin
    assert result.paintable_sink is elements[
        "gtk4paintablesink",
        "clipper-test-paintable",
    ]
    assert [element.factory for element in sink_bin.children] == [
        "glupload",
        "glcolorconvert",
        "capsfilter",
        "glshader",
        "capsfilter",
        "gtk4paintablesink",
    ]
    assert all(
        element.linked_to == [downstream]
        for element, downstream in zip(
            sink_bin.children,
            sink_bin.children[1:],
            strict=False,
        )
    )
    shader = elements["glshader", "clipper-test-sdr-transfer"]
    assert shader.properties["fragment"] == video_output.BT1886_TO_SRGB_FRAGMENT
    for caps_name in ("clipper-test-gl-rgba", "clipper-test-srgb-output"):
        assert elements["capsfilter", caps_name].properties["caps"].description == (
            video_output.SRGB_GL_CAPS
        )
    assert "memory:GLMemory" in video_output.SRGB_GL_CAPS
    assert "colorimetry=1:1:7:1" in video_output.SRGB_GL_CAPS
    assert ghost_pads == [
        ("sink", elements["glupload", "clipper-test-gl-upload"].sink_pad)
    ]
    assert len(sink_bin.pads) == 1


def test_clipper_video_sink_falls_back_when_gl_element_is_unavailable(monkeypatch):
    _sink_bin, elements, _ghost_pads = install_fake_gstreamer(
        monkeypatch,
        missing_factory="glshader",
    )

    result = video_output.make_clipper_video_sink("clipper-test")

    direct_sink = elements["gtk4paintablesink", "clipper-test-paintable"]
    assert result == video_output.ClipperVideoSink(direct_sink, direct_sink, False)


def test_clipper_video_sink_detaches_gtk_sink_after_link_failure(monkeypatch):
    sink_bin, elements, _ghost_pads = install_fake_gstreamer(
        monkeypatch,
        failed_link="glshader",
    )

    result = video_output.make_clipper_video_sink("clipper-test")

    direct_sink = elements["gtk4paintablesink", "clipper-test-paintable"]
    assert result == video_output.ClipperVideoSink(direct_sink, direct_sink, False)
    assert direct_sink.parent is None
    assert direct_sink not in sink_bin.children


def test_bt1886_shader_maps_clipper_mid_gray_to_srgb_without_moving_endpoints():
    def transform(code_value):
        encoded = code_value / 255
        linear = encoded**video_output.BT1886_GAMMA
        if linear < video_output.SRGB_LINEAR_THRESHOLD:
            output = video_output.SRGB_LINEAR_SCALE * linear
        else:
            output = (
                video_output.SRGB_POWER_SCALE
                * linear ** (1 / video_output.BT1886_GAMMA)
                - video_output.SRGB_POWER_OFFSET
            )
        return round(output * 255)

    assert transform(0) == 0
    assert transform(128) == 121
    assert transform(255) == 255
    assert "pow(max(rgba.rgb" in video_output.BT1886_TO_SRGB_FRAGMENT
    assert "step(vec3(0.0031308), linear_rgb)" in video_output.BT1886_TO_SRGB_FRAGMENT
