from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = (
    REPO_ROOT
    / "packaging"
    / "flatpak"
    / "io.github.leesethefox.Clipper.yml"
)
OBS_PATCH = (
    REPO_ROOT
    / "packaging"
    / "flatpak"
    / "patches"
    / "obs-low-resource-video.patch"
)
VKC_PATCH = (
    REPO_ROOT
    / "packaging"
    / "flatpak"
    / "patches"
    / "obs-vkcapture-lazy-cursor.patch"
)
ENGINE_SOURCE = REPO_ROOT / "engine" / "src" / "clipper_engine.c"


def _added_patch_lines(path: Path) -> str:
    return "\n".join(
        line[1:]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def test_flatpak_manifest_applies_low_resource_dependency_patches():
    manifest = MANIFEST.read_text(encoding="utf-8")

    assert OBS_PATCH.exists()
    assert VKC_PATCH.exists()
    assert "path: patches/obs-low-resource-video.patch" in manifest
    assert "path: patches/obs-vkcapture-lazy-cursor.patch" in manifest

    obs_module = manifest.index("  - name: obs-studio-libobs")
    vkc_module = manifest.index("  - name: obs-vkcapture")
    audio_module = manifest.index("  - name: obs-pipewire-audio-capture")
    assert obs_module < manifest.index("path: patches/obs-low-resource-video.patch") < vkc_module
    assert (
        vkc_module
        < manifest.index("path: patches/obs-vkcapture-lazy-cursor.patch")
        < audio_module
    )


def test_obs_patch_keeps_full_backpressure_capacity_but_starts_with_small_pool():
    added = _added_patch_lines(OBS_PATCH)

    assert "#define NUM_ENCODE_TEXTURES_MIN 4" in added
    assert "#define NUM_ENCODE_TEXTURES_MAX 10" in added
    assert "grow_gpu_encoding_texture_pool" in added
    assert "gpu_encoder_texture_count" in added
    assert "NUM_ENCODE_TEXTURES_MAX * sizeof(struct obs_tex_frame)" in added


def test_obs_patch_does_not_allocate_unused_same_size_output_texture():
    added = _added_patch_lines(OBS_PATCH)

    assert "video_output_matches_base_resolution" in added
    assert "if (video_output_matches_base_resolution(mix))" in added
    assert "if (!video_output_matches_base_resolution(video))" in added
    assert "video->output_texture = NULL;" in added


def test_vkcapture_patch_initializes_cursor_support_only_when_requested():
    added = _added_patch_lines(VKC_PATCH)

    assert "if (!was_show_cursor && ctx->show_cursor)" in added
    assert "cursor_create(ctx);" in added
    assert "else if (was_show_cursor && !ctx->show_cursor)" in added
    assert "cursor_destroy(ctx);" in added
    assert "ctx->xcursor = NULL;" in added


def test_engine_reuses_preview_gpu_resources_between_equal_size_requests():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")

    assert "preview_texrender" in source
    assert "preview_stagesurf" in source
    assert "ensure_preview_resources" in source
    assert 'cJSON_AddBoolToObject(r, "resources_reused", capture.resources_reused);' in source


def test_engine_bounds_replay_memory_and_uses_realtime_software_presets():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")

    assert 'state->max_size_mb = 1024;' in source
    assert (
        'obs_data_set_int(settings,    "max_size_mb",  state->max_size_mb);'
        in source
    )
    assert 'obs_data_set_string(settings, "preset", "veryfast");' in source
    assert 'obs_data_set_int(settings, "preset", 10);' in source


def test_engine_lets_obs_resolve_the_automatic_vaapi_device():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")

    assert 'strcmp(vaapi_device, "auto") != 0' in source
    assert 'strncpy(state->vaapi_device, "auto"' in source


def test_engine_supports_native_x11_display_capture():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")

    assert '"linux-capture",' in source
    assert 'return "xshm_input";' in source
    assert 'obs_data_set_int(display_settings, "screen", 0);' in source


def test_engine_default_replay_duration_matches_ui_default():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")

    assert "state->max_time_sec = 60;" in source
    assert "state->max_time_sec = (v > 0) ? v : 60;" in source
