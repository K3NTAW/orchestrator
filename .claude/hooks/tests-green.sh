#!/usr/bin/env bash
# TaskCompleted gate (also run externally by the Planner): tests + typecheck + lint. Exit 2 with failures-only output.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat 2>/dev/null || true)
cwd=$(jq -r '.cwd // empty' <<<"$in" 2>/dev/null); [ -n "${1:-}" ] && cwd=$1
cd "${cwd:-$PWD}"
root=${cwd:-$PWD}; sid=$(jq -r '.session_id // "cli"' <<<"$in" 2>/dev/null); sid=${sid:-cli}
task=$(jq -r '.task_id // .task.id // empty' <<<"$in" 2>/dev/null)
task=${task:-$(basename "$PWD")}; task=$(printf '%s' "$task" | tr '/ ' '__')
out=$(mktemp); rc=0
started=$(date +%s)
run() { echo "## $*" >>"$out"; "$@" >>"$out" 2>&1 || rc=1; }
if [ -x .orchestrator/tests.sh ]; then run .orchestrator/tests.sh
elif [ -f package.json ]; then
  run npm test --silent -- --watch=false
  [ -f tsconfig.json ] && run npx tsc --noEmit
  jq -e '.scripts.lint' package.json >/dev/null 2>&1 && run npm run lint --silent
elif [ -f pyproject.toml ] || ls tests/test_*.py >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1 && [ -f pyproject.toml ]; then
    if uv run --project . python -c "import pytest" >/dev/null 2>&1; then cmd=(uv run --project . pytest -q -x --no-header -p no:cacheprovider)
    else cmd=(uv run --project . python -m unittest discover -q tests); fi
  elif command -v pytest >/dev/null 2>&1; then cmd=(pytest -q -x --no-header -p no:cacheprovider)
  else cmd=(python3 -m unittest discover -q tests); fi
  if [ "${TESTS_GREEN_DRY:-}" = "1" ]; then rm -f "$out"; echo "${cmd[*]}"; exit 0; fi
  run "${cmd[@]}"
else echo "tests-green: no test runner detected in $PWD" >&2; exit 0; fi
[ $rc -eq 0 ] && {
  n=$(grep -Eo '[0-9]+ tests? (passed|run)|Ran [0-9]+ tests?' "$out" | grep -Eo '[0-9]+' | tail -1)
  n=${n:-$(grep -Eo '[0-9]+ passed' "$out" | grep -Eo '[0-9]+' | tail -1)}; n=${n:--}
  elapsed=$(( $(date +%s) - started )); sha=$(git rev-parse --short=7 HEAD 2>/dev/null || true); [ -n "$sha" ] || sha=-------
  printf 'tests-green: OK %s %s %ss\n' "$n" "$sha" "$elapsed" >&2
  rm -f "$out"; exit 0
}
fail=$(grep -E 'FAIL|ERROR|Error|error TS|✗|✕|AssertionError|assert |Traceback' "$out" | grep -v '^## ' | head -40)
mkdir -p "$root/.orchestrator/runs/tests"
log="$root/.orchestrator/runs/tests/$task-$(date +%Y%m%d-%H%M%S).log"
cp "$out" "$log"
{ echo "tests-green: FAILED. Failures only:"; printf '%s\n' "${fail:-$(tail -40 "$out")}"; echo "$log"; } >&2
rm -f "$out"; exit 2
