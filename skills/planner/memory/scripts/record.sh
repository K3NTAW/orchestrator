#!/usr/bin/env bash
# record.sh draft <GOAL_ID> | record.sh add --file F --type T --title "..." --goal T-xxxx [--tasks ..] [--fact ..]... [--outcome ..] | record.sh set architecture < summary.md
set -u; . "$(dirname "$0")/_lib.sh"
ORCH_ROOT="$(orch_root)" exec python3 "$(dirname "$0")/record.py" "$@"
