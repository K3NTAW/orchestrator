#!/usr/bin/env bash
# PreToolUse Edit|Write: block writes outside the task's scope globs. Exit 2 = blocked, stderr goes back to the agent.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
file=$(jq -r '.tool_input.file_path // .tool_input.path // empty' <<<"$in")
[ -z "$file" ] && exit 0
task=$(task_id); [ -z "$task" ] && exit 0            # planner session: no task scope, allow
root=$(orch_root); tj="$root/.orchestrator/tasks/$task.json"
[ -f "$tj" ] || exit 0
wt=$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)
# normalize to physical paths (macOS /var -> /private/var); works for not-yet-existing files
[[ "$file" == /* ]] && file=$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$file")
rel=${file#"$wt"/}
while IFS= read -r glob; do
  [ -z "$glob" ] && continue
  # bash [[ == ]] pattern: * also matches '/', so src/auth/** matches any depth
  [[ "$rel" == $glob ]] && exit 0
done < <(jq -r '.scope[]?' "$tj")
echo "scope-guard: $rel is outside task $task scope: $(jq -c '.scope' "$tj"). Edit only in-scope paths or report blocked." >&2
exit 2
