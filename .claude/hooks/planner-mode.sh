#!/usr/bin/env bash
# PreToolUse Bash|Edit|Write|NotebookEdit — Planner session only (no task id). The Planner plans; workers write code.
# Allowed writes: .orchestrator/** (plan.md, memory via record.sh) and temp dirs. Everything else is a bus task.
# Escape hatch for a human-driven session: ORCH_PLANNER_MODE=0 f orch
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ -n "$(task_id)" ] && exit 0                    # worker session: scope-guard owns this
[ "${ORCH_PLANNER_MODE:-1}" = 0 ] && exit 0
tool=$(jq -r '.tool_name // ""' <<<"$in")
root=$(orch_root)
deny() {
  echo "planner-mode: BLOCKED — $1. The Planner never edits source. It may commit, branch and push; every commit message states what and why, and decisions.md gets a dated entry with the revert path. Write a spec (skill write-spec), create the bus task, hand it to codex(); if status() says Codex is cooling, executor_fallback(complexity) names the Claude tier that executes. Writes allowed here: .orchestrator/** and temp dirs only." >&2
  exit 2
}
allowed_path() {
  case "$1" in
    "$root"/.orchestrator/*|.orchestrator/*|/private/tmp/*|/tmp/*|/private/var/folders/*|/var/folders/*) return 0;;
  esac
  [ -n "${TMPDIR:-}" ] && [[ "$1" == "${TMPDIR%/}"/* ]] && return 0
  return 1
}
if [ "$tool" != "Bash" ]; then
  f=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$in"); [ -z "$f" ] && exit 0
  allowed_path "$f" || deny "$tool to $f"
  exit 0
fi
cmd=$(jq -r '.tool_input.command // ""' <<<"$in")
# 1) history rewriting is the merge queue's and the workers' job. commit/add/branch/push are allowed (guardrails.sh still blocks force-push and push to main).
if grep -qE '(^|[;&|[:space:]])git[[:space:]]+(-C[[:space:]]+[^[:space:]]+[[:space:]]+)?(merge|rebase|cherry-pick|apply|am|reset[[:space:]]+--hard|filter-branch)([[:space:]]|$)' <<<"$cmd"; then
  deny "git write command: $(head -c 100 <<<"$cmd")"
fi
# 2) file writes from bash: redirects (after dropping fd/devnull redirects), tee, sed -i, cp/mv/ln/touch/mkdir/chmod/rm, python writes
stripped=$(sed -E 's/[0-9]*>&[0-9]+//g; s/[0-9]*>[[:space:]]*\/dev\/null//g; s/<<-?[[:space:]]*['"'"'"]?[A-Za-z_]+['"'"'"]?//g' <<<"$cmd")
writes=0
grep -qE '(^|[^<>])>{1,2}[^&>]' <<<"$stripped" && writes=1
grep -qE '(^|[;&|[:space:]])(tee|sed[[:space:]]+-[a-zA-Z]*i|cp|mv|ln|touch|mkdir|chmod|rm|install|patch|truncate)[[:space:]]' <<<"$stripped" && writes=1
grep -qE 'write_text\(|write_bytes\(|open\([^)]*['"'"'"][wax]' <<<"$stripped" && writes=1
[ $writes = 0 ] && exit 0
# a write that only touches allowed places passes; anything mentioning a repo path does not
grep -qE '(^|[[:space:]"'"'"'=])(\./)?\.orchestrator/|/private/tmp/|/tmp/|/var/folders/|\$TMPDIR|\$\{TMPDIR' <<<"$stripped" && exit 0
deny "bash write outside .orchestrator/: $(head -c 100 <<<"$cmd")"
