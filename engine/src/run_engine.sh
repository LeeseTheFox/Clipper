#!/bin/bash
# run_engine.sh — launch clipper-engine with the correct library paths
#
# Reuses the lib_shim/ directory from engine/spike/ (same Flatpak OBS libs
# needed).  Build it first with:  bash engine/spike/setup_lib_shim.sh
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "$BASH_SOURCE")" && pwd)"
SPIKE_DIR="$SCRIPT_DIR/../spike"

# Ensure the lib shim (versioned codec/pulse symlinks) exists.
if [ ! -d "$SPIKE_DIR/lib_shim" ]; then
    echo "[run_engine] Building lib_shim..." >&2
    bash "$SPIKE_DIR/setup_lib_shim.sh" >&2
fi

OBS_LIB="/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
OBS_SHARE="/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/share/obs"
VKC_EXT="/var/lib/flatpak/runtime/com.obsproject.Studio.Plugin.OBSVkCapture/x86_64/stable/active/files"
SHIM="$SPIKE_DIR/lib_shim"
export CLIPPER_OBS_LIBDIR="$OBS_LIB"
export CLIPPER_OBS_DATADIR="$OBS_SHARE"
export CLIPPER_VKCAPTURE_PLUGIN="$VKC_EXT/lib/obs-plugins/linux-vkcapture.so"
export CLIPPER_VKCAPTURE_PLUGIN_DATA="$VKC_EXT/share/obs/obs-plugins/linux-vkcapture"
export LD_LIBRARY_PATH="$OBS_LIB:$SHIM:$SHIM/pulseaudio:${LD_LIBRARY_PATH:-}"

# Do NOT use software rendering — vkcapture DMA-BUF requires hardware GPU.
# Mesa/libobs can use the GPU via EGL even without a display server in most cases.
# export LIBGL_ALWAYS_SOFTWARE=1

exec "$SCRIPT_DIR/clipper-engine" "$@"
