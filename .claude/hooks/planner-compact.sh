#!/usr/bin/env bash
# SessionStart(compact) — restore the Planner's current state after in-place compaction.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ -n "$(task_id)" ] && exit 0
task=$(jq -r '.task_id // ""' <<<"$in")
[ -n "$task" ] && exit 0
[ "${ORCH_PLANNER_MODE:-1}" = 0 ] && exit 0
root=$(orch_root)
out=$(cd "$root" && perl -e 'alarm shift; exec @ARGV' 5 uv run orchestrator planner-context --brief 2>/dev/null) || true
[ -n "${out:-}" ] && echo "$out"
exit 0
