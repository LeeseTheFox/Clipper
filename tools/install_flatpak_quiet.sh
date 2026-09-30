#!/usr/bin/env bash
set -euo pipefail

app_id="io.github.leesethefox.Clipper"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

printf 'Installing...\n'

if ! command -v flatpak >/dev/null 2>&1; then
    printf 'error: flatpak command not found.\n' >&2
    exit 1
fi

# Export and prepare the bundle before interrupting the installed application.
# The build repository has no published URL, so the exported bundle does not
# configure an application update repository. Do not pass --repo-url=: Flatpak
# treats that as an instruction to clear an existing origin's URL on import.
temporary_dir="$(mktemp -d)"
bundle="$temporary_dir/Clipper.flatpak"
log_file="$temporary_dir/install.log"
trap 'rm -rf "$temporary_dir"' EXIT

fail() {
    printf 'error: %s\n' "$1" >&2
    tail -n 8 "$log_file" >&2
    exit 1
}

if ! flatpak build-bundle "$repo_root/repo" "$bundle" \
    "$app_id" master >"$log_file" 2>&1; then
    fail 'could not export the local Clipper bundle.'
fi

if ! flatpak --user install --no-deploy --or-update --assumeyes \
    --noninteractive --bundle "$bundle" >"$log_file" 2>&1; then
    fail 'could not prepare Clipper and its runtime dependencies.'
fi

migrate_origin=false
if origin="$(flatpak --user info --show-origin "$app_id" 2>/dev/null)"; then
    if ! remotes="$(flatpak --user remotes --columns=name,url 2>"$log_file")"; then
        fail 'could not inspect the installed Clipper update source.'
    fi
    origin_url="$(awk -v origin="$origin" '$1 == origin { print $2 }' <<<"$remotes")"
    if [[ -n "$origin_url" ]]; then
        migrate_origin=true
    fi
fi

if ! running_apps="$(flatpak ps --columns=instance,application 2>/dev/null)"; then
    printf 'error: could not inspect running Flatpak apps.\n' >&2
    exit 1
fi

mapfile -t running_instances < <(
    awk -v app_id="$app_id" '$2 == app_id { print $1 }' <<<"$running_apps"
)
for instance_id in "${running_instances[@]}"; do
    if ! flatpak kill "$instance_id" >/dev/null 2>&1; then
        printf 'error: could not close Clipper.\n' >&2
        exit 1
    fi
done

# Bundle --reinstall preserves an existing repository origin. A normal
# uninstall is needed once to let Flatpak create its URL-less bundle origin.
# Never use --delete-data: settings, drafts, caches and exported hooks survive.
if [[ "$migrate_origin" == true ]]; then
    if ! flatpak --user uninstall --assumeyes --noninteractive --no-related \
        --keep-ref "$app_id" >"$log_file" 2>&1; then
        fail 'could not migrate Clipper to bundle updates; application data is retained.'
    fi
fi

if ! flatpak --user install --reinstall --assumeyes --noninteractive \
    --bundle "$bundle" >"$log_file" 2>&1; then
    fail 'could not install Clipper; application data is retained. Retry this script.'
fi

printf 'Installed Clipper successfully.\n'
