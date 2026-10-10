#!/usr/bin/env bash
# PreToolUse Edit|Write: block writes outside the task's scope globs. Exit 2 = blocked, stderr goes back to the agent.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
file=$(jq -r '.tool_input.file_path // .tool_input.path // empty' <<<"$in")
[ -z "$file" ] && exit 0
payload_cwd=$(jq -r '.cwd // empty' <<<"$in")
task=${ORCH_TASK_ID:-}
if [ -z "$task" ]; then
  task=$(python3 - "$file" "$payload_cwd" <<'PY'
import os, re, sys

for value in sys.argv[1:]:
    if not value:
        continue
    path = os.path.realpath(value if os.path.isabs(value) else os.path.join(os.getcwd(), value))
    match = re.search(r"(?:^|/)wt/(T-[0-9]+)(?:/|$)", path)
    if match:
        print(match.group(1))
        break
PY
  )
fi
if [ -z "$task" ]; then
  task=$(task_id)
fi
[ -z "$task" ] && exit 0                            # planner session: no task scope, allow
root=$(orch_root); tj="$root/.orchestrator/tasks/$task.json"
[ -f "$tj" ] || exit 0
# Normalize to physical paths (macOS /var -> /private/var); works for not-yet-existing files.
file=$(python3 - "$file" "$payload_cwd" <<'PY'
import os, sys

path, cwd = sys.argv[1:]
base = os.path.realpath(cwd or os.getcwd())
print(os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path)))
PY
)

# Probe the repository containing the file and compare its shared Git directory to the task worktree.
probe=$(python3 - "$file" <<'PY'
import os, sys

path = sys.argv[1]
while not os.path.isdir(path):
    parent = os.path.dirname(path)
    if parent == path:
        break
    path = parent
print(path)
PY
)
containing=
common=
while IFS= read -r line; do
  if [ -z "$containing" ]; then containing=$line; else common=$line; fi
done < <(git -C "$probe" rev-parse --show-toplevel --git-common-dir 2>/dev/null || true)
task_wt=$(jq -r '.worktree // empty' "$tj")
if [ -z "$task_wt" ]; then
  task_wt=$(git rev-parse --show-toplevel 2>/dev/null || true)
fi
task_common=$(git -C "$task_wt" rev-parse --git-common-dir 2>/dev/null || true)
if [ -z "${containing:-}" ] || [ -z "${common:-}" ] || [ -z "$task_wt" ] || [ -z "$task_common" ]; then
  rel=$file
else
  common=$(python3 - "$probe" "$common" <<'PY'
import os, sys
base, path = sys.argv[1:]
print(os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path)))
PY
  )
  task_common=$(python3 - "$task_wt" "$task_common" <<'PY'
import os, sys
base, path = sys.argv[1:]
print(os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path)))
PY
  )
  if [ "$common" != "$task_common" ]; then
    rel=$file
  else
    rel=${file#"$containing"/}
  fi
fi
if [ "$rel" = "$file" ] && { [ -z "${containing:-}" ] || [ "$common" != "$task_common" ]; }; then
  echo "scope-guard: $rel is outside task $task scope: $(jq -c '{scope: .scope, grant_scope: (.constraints.grant_scope // [])}' "$tj"). Edit only in-scope paths or report blocked." >&2
  exit 2
fi
while IFS= read -r glob; do
  [ -z "$glob" ] && continue
  # bash [[ == ]] pattern: * also matches '/', so src/auth/** matches any depth
  [[ "$rel" == $glob ]] && exit 0
done < <(jq -r '.scope[]?, .constraints.grant_scope[]?' "$tj")
echo "scope-guard: $rel is outside task $task scope: $(jq -c '{scope: .scope, grant_scope: (.constraints.grant_scope // [])}' "$tj"). Edit only in-scope paths or report blocked." >&2
exit 2
