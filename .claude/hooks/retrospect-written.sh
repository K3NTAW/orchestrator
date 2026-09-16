#!/usr/bin/env bash
# TaskCompleted on a parent goal (subject starts with GOAL): block until memory has a dated entry or plan.md says "no learnings".
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
subj=$(jq -r '.task_input.subject // .task.subject // .subject // ""' <<<"$in")
grep -qiE '^goal' <<<"$subj" || exit 0
root=$(orch_root); today=$(date +%F)
grep -rqs "$today" "$root/.orchestrator/memory/" && exit 0
grep -qis 'no learnings' "$root/.orchestrator/plan.md" && exit 0
echo "retrospect-written: no memory entry dated $today in .orchestrator/memory/ and plan.md lacks 'no learnings'. Write the retrospective first." >&2
exit 2
