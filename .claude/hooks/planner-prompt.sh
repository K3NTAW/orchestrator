#!/usr/bin/env bash
# UserPromptSubmit — Planner session only. stdout lands in the model's context on every user message.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ -n "$(task_id)" ] && exit 0
[ "${ORCH_PLANNER_MODE:-1}" = 0 ] && exit 0
p=$(jq -r '.prompt // ""' <<<"$in")
case "$p" in /*) exit 0;; esac   # already a skill invocation
echo "PLANNER MODE (hook): unless this message is a pure question about state, it is a goal. First tool call: Skill(orchestrate). Changes to the orchestrator itself (skills, docs, hooks, configs) are goals too. You cannot edit source or commit: planner-mode.sh blocks it. Codex cooling → executor_fallback(complexity) → a Claude tier executes; never do the work yourself."
