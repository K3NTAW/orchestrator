#!/usr/bin/env bash
# PreToolUse Bash|Edit|Write|NotebookEdit. Agents run with permissions bypassed; this is the floor they cannot go below.
# Blocks: protected paths (.orchestrator/protected-paths.txt), destructive/system commands, recursive rm outside work dirs.
set -u; . "$(dirname "$0")/_lib.sh"
in=$(cat); tool=$(jq -r '.tool_name // ""' <<<"$in")
root=$(orch_root); list="$root/.orchestrator/protected-paths.txt"
[ -f "$list" ] || list="$(dirname "$0")/../../.orchestrator/protected-paths.txt"
deny() { echo "guardrails: BLOCKED — $1. This is a hard floor (see .orchestrator/protected-paths.txt); ask the human." >&2; exit 2; }
canon() { python3 -c 'import os,sys;print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$1"; }
# one python call for the whole list (a per-path call cost ~1s per hook invocation)
protected=(); while IFS= read -r c; do protected+=("$c"); done < <(grep -vE '^\s*(#|$)' "$list" | python3 -c 'import os,sys;[print(os.path.realpath(os.path.expanduser(l.rstrip("\n")))) for l in sys.stdin]')
hits_protected() { local p=$1 c; for c in "${protected[@]}"; do [[ "$p" == "$c" || "$p" == "$c"/* ]] && return 0; done; return 1; }

if [[ "$tool" != "Bash" ]]; then
  f=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$in"); [ -z "$f" ] && exit 0
  hits_protected "$(canon "$f")" && deny "$f is a protected path"
  exit 0
fi

cmd=$(jq -r '.tool_input.command // ""' <<<"$in")
# 1) destructive / system-level commands
if grep -qE '(^|[;&|[:space:]])(sudo|su|diskutil|mkfs|newfs|shutdown|reboot|halt|launchctl[[:space:]]+(bootout|unload|remove|disable)|csrutil|nvram|spctl|tmutil[[:space:]]+delete|killall[[:space:]]+(Finder|Dock|loginwindow|WindowServer))([[:space:]]|$)' <<<"$cmd"; then
  deny "system-level command: $(head -c 120 <<<"$cmd")"; fi
grep -qE 'dd[[:space:]]+if=|>[[:space:]]*/dev/(disk|rdisk|sd)|chmod[[:space:]]+(-R[[:space:]]+)?[0-7]*777[[:space:]]+/([[:space:]]|$)|chown[[:space:]]+-R[[:space:]]+[^[:space:]]+[[:space:]]+/([[:space:]]|$)' <<<"$cmd" && deny "raw disk / recursive perms on /"
grep -qE 'security[[:space:]]+(find-(generic|internet)-password|dump-keychain|delete-|export|set-key-partition)' <<<"$cmd" && deny "Keychain access"
grep -qE 'git[[:space:]]+push[^|;&]*([[:space:]]-f([[:space:]]|$)|--force([[:space:]]|$)|\+[a-zA-Z])' <<<"$cmd" && deny "force push (open a PR instead)"
grep -qE 'git[[:space:]]+push[^|;&]*([[:space:]]origin[[:space:]]+main|[[:space:]]origin[[:space:]]+master)([[:space:]]|$)' <<<"$cmd" && deny "direct push to main (open a PR instead)"
grep -qE 'osascript[^|;&]*(empty trash|delete|System Events)' <<<"$cmd" && deny "osascript against system/Finder"
grep -qiE '(gh|curl|mail|sendmail|osascript)[^|;&]*(send|mail|smtp)' <<<"$cmd" && grep -qiE 'mail|smtp|--body|Message' <<<"$cmd" && deny "sending mail needs human approval"
# 2) protected paths anywhere in the command (both ~ and expanded forms)
while IFS= read -r l; do [[ -z "$l" || "$l" == \#* ]] && continue
  exp=${l/#\~/$HOME}; if grep -qF -e "$l" -e "$exp" <<<"$cmd"; then deny "touches protected path $l"; fi
done <"$list"
# 3) recursive rm: every target must resolve under a work dir
if grep -qE '(^|[;&|[:space:]])rm[[:space:]]+-[a-zA-Z]*[rR]' <<<"$cmd"; then
  allowed=("$(canon ~/code)" /private/tmp /tmp "$(canon /var/folders)" "$(canon "${TMPDIR:-/tmp}")")
  for tgt in $(sed -E 's/.*rm[[:space:]]+(-[a-zA-Z]+[[:space:]]+)*//' <<<"$cmd" | tr -d '"'"'"); do
    [[ "$tgt" == -* ]] && continue
    c=$(cd "$(jq -r '.cwd // "."' <<<"$in")" 2>/dev/null && canon "$tgt"); ok=0
    for a in "${allowed[@]}"; do [[ "$c" == "$a"/* ]] && ok=1; done
    [[ "$c" == "$(canon ~/code)" || "$c" == "$HOME" || "$c" == "/" ]] && ok=0
    [ $ok = 1 ] || deny "recursive rm outside work dirs: $tgt -> $c"
  done
fi
exit 0
