#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
manifest="packaging/flatpak/io.github.leesethefox.Clipper.yml"
builder_app="org.flatpak.Builder"

for command in flatpak systemd-run; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "error: required command not found: $command" >&2
        exit 1
    fi
done

if [[ ! -f "$repo_root/$manifest" ]]; then
    echo "error: Flatpak manifest not found: $manifest" >&2
    exit 1
fi

if ! flatpak --user info "$builder_app" >/dev/null 2>&1; then
    echo "error: expected the user Flatpak $builder_app to be installed" >&2
    exit 1
fi

log_file="$(mktemp)"
cleanup() {
    rm -f "$log_file"
}
trap cleanup EXIT

start_seconds=$SECONDS
pushd "$repo_root" >/dev/null
set +e
systemd-run \
    --user \
    --scope \
    -p MemoryMax=6G \
    -p MemorySwapMax=6G \
    -p CPUWeight=50 \
    env CMAKE_BUILD_PARALLEL_LEVEL=1 MAKEFLAGS=-j1 \
    flatpak run --command=flatpak-builder "$builder_app" \
    --user \
    --force-clean \
    --disable-updates \
    --disable-rofiles-fuse \
    --jobs=1 \
    --state-dir=.flatpak-builder \
    --repo=repo \
    build-dir-bounded \
    "$manifest" >"$log_file" 2>&1
build_status=$?
set -e
popd >/dev/null

elapsed_seconds=$((SECONDS - start_seconds))
line_count="$(wc -l <"$log_file")"

if [[ "$build_status" -eq 0 ]]; then
    mapfile -t commits < <(sed -n 's/^Commit: //p' "$log_file")
    echo "Flatpak build result: PASS"
    echo "Elapsed: ${elapsed_seconds}s"
    if [[ "${#commits[@]}" -ge 1 ]]; then
        echo "App commit: ${commits[0]}"
    fi
    if [[ "${#commits[@]}" -ge 2 ]]; then
        echo "Debug commit: ${commits[1]}"
    fi
    echo "Verbose output suppressed: ${line_count} lines"
    exit 0
fi

echo "Flatpak build result: FAIL"
echo "Exit code: $build_status"
echo "Elapsed: ${elapsed_seconds}s"
echo "Captured output: ${line_count} lines"

if grep -E -i -q \
    '(^|[^[:alpha:]])(error|fatal|failed|failure)(:|[^[:alpha:]])|not found|no such file' \
    "$log_file"; then
    echo
    echo "Likely failure lines (last 20):"
    grep -E -i \
        '(^|[^[:alpha:]])(error|fatal|failed|failure)(:|[^[:alpha:]])|not found|no such file' \
        "$log_file" | tail -n 20 | cut -c1-400
fi

echo
echo "Final build output (last 40 lines):"
tail -n 40 "$log_file" | cut -c1-400
exit "$build_status"
