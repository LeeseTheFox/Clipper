"""Extract packaged C implementations for native tests and clangd."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {
    "idle_render_under_test.h": (("obs-idle-render.patch", "clipper_defer_idle_render"),),
    "native_capture_under_test.h": (
        ("obs-native-capture.patch", "obs_scene_native_capture"),
        ("obs-native-capture.patch", "native_capture_single_video_channel"),
        ("obs-vkcapture-native-texture.patch", "vkcapture_native_texture"),
    ),
    "direct_queue_under_test.h": (
        ("obs-vaapi-direct-surfaces.patch", "release_tex_frame_surface"),
        ("obs-vaapi-direct-surfaces.patch", "render_direct_surface"),
        ("obs-vaapi-direct-surfaces.patch", "vaapi_direct_cache_release"),
    ),
    "direct_validation_under_test.h": (
        ("obs-vaapi-direct-surfaces.patch", "vaapi_direct_validate"),
        ("obs-vaapi-direct-surfaces.patch", "vaapi_direct_wait"),
    ),
    "lazy_buffers_under_test.h": (
        ("obs-lazy-video-buffers.patch", "grow_gpu_encoding_texture_pool"),
        ("obs-lazy-video-buffers.patch", "ensure_gpu_encoding_texture"),
        ("obs-lazy-video-buffers.patch", "init_cache"),
    ),
    "pipewire_cache_under_test.h": (
        ("obs-pipewire-import-cache.patch", "dmabuf_import"),
        ("obs-pipewire-import-cache.patch", "clear_frame_sync"),
        ("obs-pipewire-import-cache.patch", "clear_current_texture"),
        ("obs-pipewire-import-cache.patch", "clear_imports"),
        ("obs-pipewire-import-cache.patch", "on_remove_buffer_cb"),
        ("obs-pipewire-import-cache.patch", "import_buffer"),
    ),
}


def generate_header(output: Path, header: str) -> None:
    """Include post-patch context as well as additions, excluding removed lines."""
    output.mkdir(parents=True, exist_ok=True)
    definitions = []
    for patch, name in HEADERS[header]:
        text = (ROOT / "packaging/flatpak/patches" / patch).read_text(encoding="utf-8")
        source = "\n".join(
            line[1:] for line in text.splitlines()
            if line.startswith((" ", "+")) and not line.startswith("+++")
        )
        symbol = re.escape(name)
        pattern = (
            rf"^struct {symbol} \{{.*?^\}};"
            rf"|^(?:static (?:inline )?)?(?:void |bool |\w+ \*){symbol}"
            r"\([^;{]*\)\n\{.*?^\}"
        )
        match = re.search(pattern, source, re.DOTALL | re.MULTILINE)
        if match is None:
            raise ValueError(f"Cannot extract {name} from {patch}")
        definitions.append(match[0])
    (output / header).write_text(
        "/* Generated from packaged patches; do not edit. */\n"
        + "\n\n".join(definitions) + "\n", encoding="utf-8",
    )


def generate(output: Path) -> None:
    for header in HEADERS:
        generate_header(output, header)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    generate(parser.parse_args().output)
