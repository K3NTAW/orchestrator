#!/usr/bin/env bash
# UserPromptSubmit — Planner session only. stdout lands in the model's context on every user message.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ -n "$(task_id)" ] && exit 0
[ "${ORCH_PLANNER_MODE:-1}" = 0 ] && exit 0
p=$(jq -r '.prompt // ""' <<<"$in")
case "$p" in /*) exit 0;; esac   # already a skill invocation
echo "Planner mode: goals go through Skill(orchestrate); never edit source; see CLAUDE.md"
# Keep prompt submission responsive and fail open if the local transcript cannot be read.
tp=$(jq -r '.transcript_path // ""' <<<"$in")
sid=$(jq -r '.session_id // ""' <<<"$in")
root=$(orch_root)
out=$(cd "$root" && perl -e 'alarm shift; exec @ARGV' 3 uv run orchestrator planner-context --hook ${tp:+--transcript "$tp"} ${sid:+--session-id "$sid"} 2>/dev/null) && [ -n "$out" ] && echo "$out"
exit 0
