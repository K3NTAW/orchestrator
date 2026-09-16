# shellcheck shell=bash
orch_root() {
  local d
  for d in "${ORCH_ROOT:-}" "$(git rev-parse --show-toplevel 2>/dev/null)/../.." "$(git rev-parse --show-toplevel 2>/dev/null)" "$PWD"; do
    [ -n "$d" ] && [ -d "$d/.orchestrator" ] && { cd "$d" && pwd; return; }
  done
  echo "$PWD"
}
task_id() {
  [ -n "${ORCH_TASK_ID:-}" ] && { echo "$ORCH_TASK_ID"; return; }
  basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" | grep -E '^T-[0-9]+$' || true
}
