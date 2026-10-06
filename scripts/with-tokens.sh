#!/usr/bin/env bash
# Run one command with selected f-tok tokens in its environment. Values reach only the child process.
#
# Usage:
#   scripts/with-tokens.sh ENV_NAME=token-id [ENV_NAME=token-id ...] -- command [args...]
#   scripts/with-tokens.sh --list    # prints the allowlisted token ids, one per line
#
# Allowlist: the only token ids allowed are the non-blank, non-# lines of <repo root>/.orchestrator/token-allowlist.txt,
# where repo root is the parent of this script's directory. The location is fixed and protected by
# .orchestrator/protected-paths.txt; there is no override, and WITH_TOKENS_ALLOWLIST in the environment is refused
# (exit 3). The human owns that file; this script never writes it. A missing or empty allowlist refuses every
# request (exit 3); an id not on it exits 3.
#
# ENV_NAME must match ^[A-Z][A-Z0-9_]*$ and may not be a shell or loader variable (PATH, HOME, LD_PRELOAD, ...);
# token-id must match ^[A-Za-z0-9._-]+$. Bad usage exits 2; a missing or empty token exits 1 naming only the id.
#
# Values are read with `f tok get <id> --reveal` into variables and exported just before exec; the script never
# prints them. The command's own output is the caller's responsibility: never run echo, printenv or env through it.
# WITH_TOKENS_F_SH overrides the f helper path and exists for tests only: a fake f.sh yields only fake values.
set -euo pipefail

usage() {
  echo "Usage: scripts/with-tokens.sh ENV_NAME=token-id [ENV_NAME=token-id ...] -- command [args...]"
  echo "       scripts/with-tokens.sh --list"
}

if [[ -n "${WITH_TOKENS_ALLOWLIST+set}" ]]; then
  echo "with-tokens: WITH_TOKENS_ALLOWLIST is not supported" >&2
  exit 3
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
allowlist_file="${repo_root}/.orchestrator/token-allowlist.txt"

allowed=()
if [[ -f "${allowlist_file}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    allowed+=("${line}")
  done <"${allowlist_file}"
fi

if [[ "${1:-}" == "--list" && $# -eq 1 ]]; then
  for id in ${allowed[@]+"${allowed[@]}"}; do
    printf '%s\n' "${id}"
  done
  exit 0
fi

names=()
ids=()
saw_dashes=0
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then
    saw_dashes=1
    shift
    break
  fi
  pair="$1"
  shift
  if [[ "${pair}" != *=* ]]; then
    usage >&2
    exit 2
  fi
  name="${pair%%=*}"
  id="${pair#*=}"
  if [[ ! "${name}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    echo "with-tokens: invalid ENV_NAME: ${name}" >&2
    exit 2
  fi
  case "${name}" in
    PATH|HOME|SHELL|IFS|BASH_ENV|ENV|LD_PRELOAD|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH|PROMPT_COMMAND)
      echo "with-tokens: refused ENV_NAME: ${name}" >&2
      exit 2 ;;
  esac
  if [[ ! "${id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "with-tokens: invalid token id" >&2
    exit 2
  fi
  names+=("${name}")
  ids+=("${id}")
done
if [[ ${saw_dashes} -eq 0 || $# -eq 0 || ${#names[@]} -eq 0 ]]; then
  usage >&2
  exit 2
fi

if [[ ${#allowed[@]} -eq 0 ]]; then
  echo "with-tokens: no allowlist" >&2
  exit 3
fi
for id in "${ids[@]}"; do
  ok=0
  for a in "${allowed[@]}"; do
    if [[ "${a}" == "${id}" ]]; then
      ok=1
      break
    fi
  done
  if [[ ${ok} -eq 0 ]]; then
    echo "with-tokens: ${id} is not allowlisted" >&2
    exit 3
  fi
done

# f installs this helper to make the `f tok` command available in shells.
source "${WITH_TOKENS_F_SH:-${HOME}/.config/f/f.sh}"

values=()
for i in "${!ids[@]}"; do
  if ! value="$(f tok get "${ids[$i]}" --reveal 2>/dev/null)" || [[ -z "${value}" ]]; then
    echo "with-tokens: missing or empty token: ${ids[$i]}" >&2
    exit 1
  fi
  values+=("${value}")
done
unset value
for i in "${!names[@]}"; do
  export "${names[$i]}=${values[$i]}"
done
unset values
exec "$@"
