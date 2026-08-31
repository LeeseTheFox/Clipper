import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shell_launchers_have_valid_syntax():
    for script in (
        REPO_ROOT / "clipper",
        REPO_ROOT / "engine" / "src" / "run_engine.sh",
        REPO_ROOT / "tools" / "run_flatpak_build_quiet.sh",
        REPO_ROOT / "tools" / "run_pytest_quiet.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_top_level_launcher_uses_packaged_flatpak_paths():
    launcher = (REPO_ROOT / "clipper").read_text(encoding="utf-8")
    installed_block = launcher.split('if [ -n "${FLATPAK_ID:-}" ]', 1)[1].split(
        "else",
        1,
    )[0]

    assert 'ENGINE_DIR="/app/libexec/clipper"' in launcher
    assert 'UI_DIR="/app/share/clipper/ui"' in launcher
    assert '[ -n "${FLATPAK_ID:-}" ]' in launcher
    assert '"$PYTHON" main.py' in launcher
    assert '"$PYTHON" main.py "$@"' in launcher
    assert "com.obsproject.Studio" not in installed_block
    assert "/var/lib/flatpak/app/com.obsproject.Studio" not in installed_block


def test_top_level_launcher_keeps_native_development_overrides():
    launcher = (REPO_ROOT / "clipper").read_text(encoding="utf-8")

    assert 'ENGINE_DIR="$SCRIPT_DIR/engine/src"' in launcher
    assert 'UI_DIR="$SCRIPT_DIR/ui"' in launcher
    assert 'export CLIPPER_OBS_LIBDIR="$OBS_LIB"' in launcher
    assert 'export CLIPPER_OBS_DATADIR="$OBS_SHARE"' in launcher
    assert (
        'export CLIPPER_VKCAPTURE_PLUGIN="$VKC_EXT/lib/obs-plugins/linux-vkcapture.so"'
        in launcher
    )
    assert (
        'export CLIPPER_VKCAPTURE_PLUGIN_DATA="$VKC_EXT/share/obs/obs-plugins/linux-vkcapture"'
        in launcher
    )


def test_flatpak_build_wrapper_is_resource_bounded_quiet_and_can_download():
    wrapper = (
        REPO_ROOT / "tools" / "run_flatpak_build_quiet.sh"
    ).read_text(encoding="utf-8")

    assert "MemoryMax=6G" in wrapper
    assert "MemorySwapMax=6G" in wrapper
    assert "CMAKE_BUILD_PARALLEL_LEVEL=1" in wrapper
    assert "MAKEFLAGS=-j1" in wrapper
    assert "--jobs=1" in wrapper
    assert "--disable-download" not in wrapper
    assert "--disable-updates" in wrapper
    assert '>"$log_file" 2>&1' in wrapper
    assert 'tail -n 40 "$log_file"' in wrapper
