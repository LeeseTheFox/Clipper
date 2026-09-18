"""Exercise the allocation helpers shipped in the OBS patch."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_lazy_video_allocation_contract(run_native_test):
    run_native_test("lazy_video_buffers.c", "lazy_buffers_under_test.h")


def test_lazy_buffers_patch_follows_direct_and_native_paths():
    manifest = (ROOT / "packaging/flatpak/io.github.leesethefox.Clipper.yml").read_text()
    assert manifest.index("patches/obs-native-capture.patch") < manifest.index(
        "patches/obs-lazy-video-buffers.patch"
    )
