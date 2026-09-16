#!/usr/bin/env bash
# TaskCreated: reject tasks without acceptance criteria and a scope list.
set -u
in=$(cat)
desc=$(jq -r '[.task_input.description, .task.description, .description, .task_input.subject, .task.subject, .subject] | map(select(. != null)) | join("\n")' <<<"$in")
miss=()
grep -qiE 'acceptance' <<<"$desc" || miss+=(acceptance)
grep -qiE 'scope' <<<"$desc" || miss+=(scope)
[ ${#miss[@]} -eq 0 ] && exit 0
echo "require-acceptance: task is missing: ${miss[*]}. Add an 'Acceptance:' list and a 'Scope:' path list to the description." >&2
exit 2
