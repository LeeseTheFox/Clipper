#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
build_root="$repo_root/.flatpak-builder/build"
include_dir="$repo_root/.flatpak-builder/clangd-include"
latest_config=""

for config in \
    "$build_root"/obs-studio-libobs-*/_flatpak_build/config/obsconfig.h; do
    if [[ ! -f "$config" ]]; then
        continue
    fi
    if [[ -z "$latest_config" || "$config" -nt "$latest_config" ]]; then
        latest_config="$config"
    fi
done

if [[ -z "$latest_config" ]]; then
    echo "error: no completed libobs Flatpak build cache found" >&2
    echo "run ./tools/run_flatpak_build_quiet.sh first" >&2
    exit 1
fi

module_build="${latest_config%/_flatpak_build/config/obsconfig.h}"
module_name="${module_build##*/}"

mkdir -p "$include_dir"
ln -sfn "../build/$module_name/libobs" "$include_dir/obs"
ln -sfn \
    "../build/$module_name/_flatpak_build/config/obsconfig.h" \
    "$include_dir/obsconfig.h"

echo "clangd libobs headers: $module_name"

# Only expose the SDK's Vulkan header namespaces. Adding its whole include
# directory would mix the SDK's libc headers with the host compiler's libc.
sdk_location=""
for installation in --user --system; do
    if sdk_location="$(flatpak "$installation" info --show-location org.freedesktop.Sdk//25.08 2>/dev/null)"; then
        break
    fi
done
if [[ -n "$sdk_location" && -d "$sdk_location/files/include/vulkan" ]]; then
    ln -sfn "$sdk_location/files/include/vulkan" "$include_dir/vulkan"
    ln -sfn "$sdk_location/files/include/vk_video" "$include_dir/vk_video"
    echo "clangd Vulkan headers: Freedesktop SDK 25.08"
else
    echo "warning: install Freedesktop SDK 25.08 for frame-harness Vulkan diagnostics" >&2
fi

"$repo_root/venv/bin/python" "$script_dir/generate_native_test_headers.py" --output "$include_dir"
echo "clangd native test headers: generated from packaged patches"
