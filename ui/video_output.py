"""Shared color-managed GTK video output for clips produced by Clipper."""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst

BT1886_GAMMA = 2.4
SRGB_LINEAR_THRESHOLD = 0.0031308
SRGB_LINEAR_SCALE = 12.92
SRGB_POWER_SCALE = 1.055
SRGB_POWER_OFFSET = 0.055

# Clipper records full-range Rec.709 SDR (GStreamer colorimetry 1:3:5:1).
# The intended display EOTF for that signal is BT.1886, conventionally gamma
# 2.4. GTK expects an sRGB-encoded texture, so carry the intended display light
# through BT.1886 decoding and the sRGB OETF explicitly. This is deliberately a
# Clipper-media output path, not a general-purpose transform for arbitrary HDR
# or differently mastered external video.
BT1886_TO_SRGB_FRAGMENT = f"""#version 100
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;

void main() {{
    vec4 rgba = texture2D(tex, v_texcoord);
    vec3 linear_rgb = pow(max(rgba.rgb, vec3(0.0)), vec3({BT1886_GAMMA}));
    vec3 linear_segment = {SRGB_LINEAR_SCALE} * linear_rgb;
    vec3 power_segment = {SRGB_POWER_SCALE} *
        pow(linear_rgb, vec3(1.0 / {BT1886_GAMMA})) - {SRGB_POWER_OFFSET};
    vec3 encoded_rgb = mix(
        linear_segment,
        power_segment,
        step(vec3({SRGB_LINEAR_THRESHOLD}), linear_rgb)
    );
    gl_FragColor = vec4(encoded_rgb, rgba.a);
}}
"""

# glshader preserves caps. Negotiate sRGB-tagged GL RGBA before it so GTK does
# not apply its own Rec.709-to-sRGB transfer after the explicit shader. Keeping
# the GLMemory feature also allows hardware decoders to stay on the DMABUF/GL
# path through glupload instead of forcing a system-memory readback.
SRGB_GL_CAPS = (
    "video/x-raw(memory:GLMemory),"
    "format=RGBA,colorimetry=1:1:7:1,texture-target=2D"
)


@dataclass(frozen=True)
class ClipperVideoSink:
    """A pipeline-facing sink plus the inner GTK sink that owns the paintable."""

    element: object
    paintable_sink: object
    color_corrected: bool


def _make_element(factory: str, name: str):
    return Gst.ElementFactory.make(factory, name)


def _make_bin(name: str):
    return Gst.Bin.new(name)


def _make_caps(description: str):
    return Gst.Caps.from_string(description)


def _make_ghost_pad(name: str, target):
    return Gst.GhostPad.new(name, target)


def _direct_sink_result(paintable_sink) -> ClipperVideoSink | None:
    if paintable_sink is None:
        return None
    return ClipperVideoSink(paintable_sink, paintable_sink, False)


def _detach_paintable_sink(sink_bin, paintable_sink) -> bool:
    """Recover the GTK sink from a partially built bin for direct fallback."""
    try:
        if paintable_sink.get_parent() is sink_bin:
            return bool(sink_bin.remove(paintable_sink))
        return paintable_sink.get_parent() is None
    except Exception:
        return False


def make_clipper_video_sink(name: str) -> ClipperVideoSink | None:
    """Build the Clipper SDR correction bin, falling back to a direct GTK sink."""
    paintable_sink = _make_element("gtk4paintablesink", f"{name}-paintable")
    if paintable_sink is None:
        return None

    sink_bin = None
    try:
        sink_bin = _make_bin(name)
        upload = _make_element("glupload", f"{name}-gl-upload")
        convert = _make_element("glcolorconvert", f"{name}-gl-convert")
        input_caps = _make_element("capsfilter", f"{name}-gl-rgba")
        shader = _make_element("glshader", f"{name}-sdr-transfer")
        output_caps = _make_element("capsfilter", f"{name}-srgb-output")
        correction_elements = (upload, convert, input_caps, shader, output_caps)
        if sink_bin is None or any(element is None for element in correction_elements):
            raise RuntimeError("required GStreamer GL elements are unavailable")

        caps = _make_caps(SRGB_GL_CAPS)
        if caps is None or caps.is_empty():
            raise RuntimeError("the GL output caps are invalid")
        input_caps.set_property("caps", caps)
        output_caps.set_property("caps", caps.copy())
        shader.set_property("fragment", BT1886_TO_SRGB_FRAGMENT)

        pipeline_elements = (*correction_elements, paintable_sink)
        for element in pipeline_elements:
            if not sink_bin.add(element):
                raise RuntimeError(f"could not add {element.get_name()} to the video bin")
        if not all(
            upstream.link(downstream)
            for upstream, downstream in zip(
                pipeline_elements,
                pipeline_elements[1:],
                strict=False,
            )
        ):
            raise RuntimeError("the color-corrected video output could not be linked")

        upload_pad = upload.get_static_pad("sink")
        ghost_pad = _make_ghost_pad("sink", upload_pad) if upload_pad is not None else None
        if ghost_pad is None or not sink_bin.add_pad(ghost_pad):
            raise RuntimeError("the color-corrected video output has no sink pad")
    except Exception as error:
        print(f"Color-managed video output unavailable ({error}); using direct output")
        if sink_bin is not None:
            try:
                sink_bin.set_state(Gst.State.NULL)
            except Exception:
                pass
        if not _detach_paintable_sink(sink_bin, paintable_sink):
            paintable_sink = _make_element(
                "gtk4paintablesink",
                f"{name}-direct-paintable",
            )
        return _direct_sink_result(paintable_sink)

    return ClipperVideoSink(sink_bin, paintable_sink, True)
