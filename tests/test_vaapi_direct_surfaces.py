"""Compile actual added queue functions, not a separate behavioral model."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "packaging/flatpak/patches/obs-vaapi-direct-surfaces.patch"


def test_direct_surface_queue_contract(run_native_test):
    result = run_native_test("vaapi_direct_queue.c", "direct_queue_under_test.h", "-pthread")
    assert "direct surface queue tests passed" in result.stdout


def test_direct_surface_patch_is_packaged_after_import_cache():
    manifest = (ROOT / "packaging/flatpak/io.github.leesethefox.Clipper.yml").read_text()
    assert manifest.index("patches/obs-vaapi-import-cache.patch") < manifest.index(
        "patches/obs-vaapi-direct-surfaces.patch")
    patch = PATCH.read_text()
    assert 'getenv("CLIPPER_VAAPI_DIRECT_SURFACES")' in patch
    assert '!direct || strcmp(direct, "0") != 0' in patch
    assert "gpu_active && !raw_active && render_direct_surface" in patch
    assert "av_frame_clone(lease->surface.frame)" in patch
    assert "glClientWaitSync" in patch


def test_direct_surface_validation_cache(run_native_test):
    run_native_test("vaapi_direct_validation.c", "direct_validation_under_test.h")
