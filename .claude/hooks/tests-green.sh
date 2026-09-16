#!/usr/bin/env bash
# TaskCompleted gate (also run externally by the Planner): tests + typecheck + lint. Exit 2 with failures-only output.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat 2>/dev/null || true)
cwd=$(jq -r '.cwd // empty' <<<"$in" 2>/dev/null); [ -n "${1:-}" ] && cwd=$1
cd "${cwd:-$PWD}"
root=$(orch_root); sid=$(jq -r '.session_id // "cli"' <<<"$in" 2>/dev/null); sid=${sid:-cli}
out=$(mktemp); rc=0
run() { echo "## $*" >>"$out"; "$@" >>"$out" 2>&1 || rc=1; }
if [ -x .orchestrator/tests.sh ]; then run .orchestrator/tests.sh
elif [ -f package.json ]; then
  run npm test --silent -- --watch=false
  [ -f tsconfig.json ] && run npx tsc --noEmit
  jq -e '.scripts.lint' package.json >/dev/null 2>&1 && run npm run lint --silent
elif [ -f pyproject.toml ] || ls tests/test_*.py >/dev/null 2>&1; then
  if command -v pytest >/dev/null 2>&1; then run pytest -q -x --no-header -p no:cacheprovider; else run python3 -m unittest discover -q tests; fi
else echo "tests-green: no test runner detected in $PWD" >&2; exit 0; fi
[ $rc -eq 0 ] && { rm -f "$out"; exit 0; }
fail=$(grep -E 'FAIL|ERROR|Error|error TS|✗|✕|AssertionError|assert |Traceback' "$out" | grep -v '^## ' | head -40)
mkdir -p "$root/.orchestrator/runs"; printf '%s\n' "$fail" | shasum | cut -c1-12 >>"$root/.orchestrator/runs/loop-$sid.log"
{ echo "tests-green: FAILED. Failures only:"; printf '%s\n' "${fail:-$(tail -40 "$out")}"; } >&2
rm -f "$out"; exit 2
