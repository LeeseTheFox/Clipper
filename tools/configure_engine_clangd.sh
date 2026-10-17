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

mkdir -p "$include_dir"
if [[ -n "$latest_config" ]]; then
    module_build="${latest_config%/_flatpak_build/config/obsconfig.h}"
    module_name="${module_build##*/}"
    ln -sfn "../build/$module_name/libobs" "$include_dir/obs"
    ln -sfn \
        "../build/$module_name/_flatpak_build/config/obsconfig.h" \
        "$include_dir/obsconfig.h"
    echo "clangd libobs headers: $module_name"
else
    # A cache-only build does not unpack module sources. Restore the installed
    # headers from its pre-cleanup snapshot, which retains generated obsconfig.h.
    cache_repo="$repo_root/.flatpak-builder/cache"
    build_arch="$(uname -m)"
    cached_refs=()
    if command -v ostree >/dev/null 2>&1 && [[ -d "$cache_repo" ]]; then
        while IFS= read -r ref; do
            if [[ "$ref" == "$build_arch"-*/build-obs-studio-libobs ]]; then
                cached_refs+=("$ref")
            fi
        done < <(ostree --repo="$cache_repo" refs)
    fi
    if [[ "${#cached_refs[@]}" -ne 1 ]]; then
        echo "error: no unambiguous libobs build cache found for clangd" >&2
        echo "run ./tools/run_flatpak_build_quiet.sh first" >&2
        exit 1
    fi
    cached_commit="$(ostree --repo="$cache_repo" rev-parse "${cached_refs[0]}")"
    header_name="obs-$cached_commit"
    if [[ ! -f "$include_dir/$header_name/obs.h" ||
          ! -f "$include_dir/$header_name/obsconfig.h" ]]; then
        ostree --repo="$cache_repo" checkout --user-mode --union \
            --subpath=/files/include/obs "$cached_commit" "$include_dir/$header_name"
    fi
    ln -sfn "$header_name" "$include_dir/obs"
    ln -sfn "$header_name/obsconfig.h" "$include_dir/obsconfig.h"
    echo "clangd libobs headers: cached module ${cached_commit:0:12}"
fi

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
