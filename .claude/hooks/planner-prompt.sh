#!/usr/bin/env bash
# UserPromptSubmit — Planner session only. stdout lands in the model's context on every user message.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ -n "$(task_id)" ] && exit 0
[ "${ORCH_PLANNER_MODE:-1}" = 0 ] && exit 0
p=$(jq -r '.prompt // ""' <<<"$in")
case "$p" in /*) exit 0;; esac   # already a skill invocation
echo "Planner mode: goals go through Skill(orchestrate); never edit source; see CLAUDE.md"
