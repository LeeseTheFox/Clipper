#!/usr/bin/env bash
set -euo pipefail

app_id="io.github.leesethefox.Clipper"
remote="clipper-local"

printf 'Installing...\n'

if ! command -v flatpak >/dev/null 2>&1; then
    printf 'error: flatpak command not found.\n' >&2
    exit 1
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

if ! flatpak --user install --reinstall -y "$remote" "$app_id" >/dev/null 2>&1; then
    printf 'error: could not reinstall Clipper from %s.\n' "$remote" >&2
    exit 1
fi

printf 'Installed Clipper successfully.\n'
