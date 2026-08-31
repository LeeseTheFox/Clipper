#!/bin/bash
set -e
# Resolve script directory BEFORE overriding LD_LIBRARY_PATH, because any
# subshell spawned afterwards would pick up Flatpak glibc and crash.
SCRIPT_DIR="$(cd -- "$(dirname -- "$BASH_SOURCE")" && pwd)"

# Ensure the lib shim (symlinks to Flatpak runtime codec/pulse libs) exists.
# It lives in engine/spike/lib_shim/ and avoids pulling in the full Flatpak
# runtime dir (which would override system glibc and cause stack-smash crashes).
if [ ! -d "$SCRIPT_DIR/lib_shim" ]; then
    echo "[run_spike] lib_shim not found — running setup_lib_shim.sh..." >&2
    bash "$SCRIPT_DIR/setup_lib_shim.sh" >&2
fi

OBS_LIB="/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
SHIM="$SCRIPT_DIR/lib_shim"
export LD_LIBRARY_PATH="$OBS_LIB:$SHIM:$SHIM/pulseaudio:$LD_LIBRARY_PATH"
export OBS_PLUGIN_PATH="$OBS_LIB/obs-plugins"
# Software GL so obs_reset_video works without a physical display/GPU context
export LIBGL_ALWAYS_SOFTWARE=1

exec "$SCRIPT_DIR/libobs_spike" "$@"
