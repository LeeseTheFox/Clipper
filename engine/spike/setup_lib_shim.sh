#!/bin/bash
# Creates a lib_shim/ directory with symlinks to only the specific Flatpak runtime
# libraries that aren't available on the host at the required SONAME versions.
# This avoids adding the full Flatpak runtime lib dir to LD_LIBRARY_PATH (which
# would override system glibc and cause stack-smash crashes).
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "$BASH_SOURCE")" && pwd)"
FP_RT="/var/lib/flatpak/runtime/org.freedesktop.Platform/x86_64/25.08/active/files/lib/x86_64-linux-gnu"
SHIM="$SCRIPT_DIR/lib_shim"

mkdir -p "$SHIM"

# libvpx.so.11 — needed by libavcodec.so.61 (system has .9)
for lib in libvpx.so.11 libvpx.so.11.0 libvpx.so.11.0.1; do
    [ -f "$FP_RT/$lib" ] && ln -sf "$FP_RT/$lib" "$SHIM/$lib" && echo "linked $lib"
done

# libtheora{enc,dec}.so.2 — needed by libavcodec.so.61 (system has .1)
for lib in libtheoraenc.so.2 libtheoraenc.so.2.2.1 \
           libtheoradec.so.2 libtheoradec.so.2.1.1 \
           libtheora.so.1 libtheora.so.1.4.1; do
    [ -f "$FP_RT/$lib" ] && ln -sf "$FP_RT/$lib" "$SHIM/$lib" && echo "linked $lib"
done

# libxml2.so.16 — needed by libavformat.so.61
for lib in libxml2.so.16 libxml2.so.16.0.4; do
    [ -f "$FP_RT/$lib" ] && ln -sf "$FP_RT/$lib" "$SHIM/$lib" && echo "linked $lib"
done

# libpulse — needed by linux-pulseaudio.so OBS plugin
for lib in libpulse.so.0 libpulse.so.0.24.3 \
           libpulse-simple.so.0 libpulse-simple.so.0.1.1 \
           libpulse-mainloop-glib.so.0 libpulse-mainloop-glib.so.0.0.6; do
    [ -f "$FP_RT/$lib" ] && ln -sf "$FP_RT/$lib" "$SHIM/$lib" && echo "linked $lib"
done

# libpulsecommon lives one level deeper
PULSE_RT="$FP_RT/pulseaudio"
mkdir -p "$SHIM/pulseaudio"
for lib in libpulsecommon-17.0.so libpulsedsp.so; do
    [ -f "$PULSE_RT/$lib" ] && ln -sf "$PULSE_RT/$lib" "$SHIM/pulseaudio/$lib" && echo "linked pulseaudio/$lib"
done

echo "lib_shim ready at $SHIM"
