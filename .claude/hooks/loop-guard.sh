#!/usr/bin/env bash
# Stop hook: if the last 3 failure signatures are identical, the agent is looping. Exit 2 forces a change of approach.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat)
[ "$(jq -r '.stop_hook_active // false' <<<"$in")" = "true" ] && exit 0
sid=$(jq -r '.session_id // "cli"' <<<"$in"); log="$(orch_root)/.orchestrator/runs/loop-$sid.log"
[ -f "$log" ] || exit 0
last3=$(tail -3 "$log"); [ "$(printf '%s\n' "$last3" | wc -l | tr -d ' ')" -lt 3 ] && exit 0
[ "$(printf '%s\n' "$last3" | sort -u | wc -l | tr -d ' ')" -eq 1 ] || exit 0
echo "loop-guard: the same failure has repeated 3 times. You are looping. Change approach, or post status=blocked to the bus and escalate." >&2
exit 2
