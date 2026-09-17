#!/usr/bin/env bash
# recall.sh index "<terms>" [--project NAME] [--limit N] | recall.sh get <id> [<id>...]
set -u; . "$(dirname "$0")/_lib.sh"
ORCH_ROOT="$(orch_root)" exec python3 "$(dirname "$0")/recall.py" "$@"
