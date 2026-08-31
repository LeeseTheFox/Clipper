#!/bin/bash
set -e
SCRIPT_DIR="$(cd -- "$(dirname -- "$BASH_SOURCE")" && pwd)"

# Ensure lib_shim exists (same shim as the existing spike)
if [ ! -d "$SCRIPT_DIR/lib_shim" ]; then
    echo "[run_vkcapture] lib_shim not found — running setup_lib_shim.sh..." >&2
    bash "$SCRIPT_DIR/setup_lib_shim.sh" >&2
fi

OBS_LIB="/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
SHIM="$SCRIPT_DIR/lib_shim"

# The vkcapture Flatpak extension also ships libobs_glcapture.so which the
# plugin loads at runtime via dlopen.  Add its lib dir too.
VKC_LIB="/var/lib/flatpak/runtime/com.obsproject.Studio.Plugin.OBSVkCapture/x86_64/stable/active/files/lib"

export LD_LIBRARY_PATH="$OBS_LIB:$VKC_LIB:$SHIM:$SHIM/pulseaudio:${LD_LIBRARY_PATH}"

# Do NOT set LIBGL_ALWAYS_SOFTWARE=1 here.
# The vkcapture DMA-BUF texture import requires a real GPU EGL context.
# This spike must be run from a live desktop session (Wayland/X11) with a
# real GPU.  obs_reset_video will use the real GPU via EGL automatically.

echo "[run_vkcapture] Starting vkcapture spike..."
echo "[run_vkcapture] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
echo ""

exec "$SCRIPT_DIR/vkcapture_spike" "$@"
