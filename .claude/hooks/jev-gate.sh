#!/usr/bin/env bash
# PreToolUse Bash|Read|Grep|Glob|Edit|Write|WebFetch|WebSearch, registered after guardrails.sh. Asks Jev
# (orchestrator.jev_gate) whether the proposed call is needed/redundant/destructive against the task's
# acceptance criteria; [jev].gate_mode = "block" denies on a confident redundant-or-unneeded verdict (exit 2),
# "log" (default) never blocks. Fails open (exit 0) on a Planner session (no task id), Jev disabled, any
# non-2 exit, or a timeout -- no GNU timeout on macOS, so the 3s wall clock is a perl alarm around exec.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
task=$(task_id); [ -z "$task" ] && exit 0
root=$(orch_root)
enabled=$(cd "$root" && uv run python -m orchestrator.jev_gate --enabled 2>/dev/null)
[ "$enabled" = "1" ] || exit 0

tmp=$(mktemp); printf '%s' "$in" >"$tmp"
out=$(cd "$root" && ORCH_TASK_ID="$task" perl -e 'alarm shift; exec @ARGV' 3 uv run python -m orchestrator.jev_gate <"$tmp" 2>&1)
rc=$?
rm -f "$tmp"
[ "$rc" -eq 2 ] && { echo "$out" >&2; exit 2; }
exit 0
