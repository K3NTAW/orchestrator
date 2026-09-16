#!/usr/bin/env bash
# TaskCompleted (Agent Teams teammate): if the task description carries "bus:T-0042", post the result to the bus.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
tid=$(jq -r '[.task_input.description, .task.description, .description, .task_input.subject, .task.subject, .subject] | map(select(. != null)) | join(" ") | capture("bus:(?<t>T-[0-9]+)").t // empty' <<<"$in")
[ -z "$tid" ] && exit 0
summary=$(jq -r '.task_input.result // .task.result // .result // .task_input.subject // .task.subject // .subject // "completed"' <<<"$in" | head -c 6000)
cd "$(orch_root)" && uv run python -m orchestrator.cli post "$tid" --summary "$summary" >/dev/null 2>&1 || true
exit 0
