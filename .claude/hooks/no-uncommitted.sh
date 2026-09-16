#!/usr/bin/env bash
# TeammateIdle: refuse to go idle with uncommitted changes in the worktree.
set -u
in=$(cat); cwd=$(jq -r '.cwd // empty' <<<"$in"); cd "${cwd:-$PWD}" 2>/dev/null || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
dirty=$(git status --porcelain | head -20); [ -z "$dirty" ] && exit 0
printf 'no-uncommitted: commit or discard before idling:\n%s\n' "$dirty" >&2
exit 2
