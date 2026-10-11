# Gate contract: ORCH_GATE_MODE and ORCH_GATE_BASE

Added 2026-10-11 (goal T-1792). Every path that starts the repo gate (`.claude/hooks/tests-green.sh`, which runs
`.orchestrator/tests.sh` when present) exports two environment variables to it:

| Variable | Values | Meaning |
|---|---|---|
| `ORCH_GATE_MODE` | `task` or `full` | `task`: the gate may run only what the diff against the base touches. `full`: run everything. |
| `ORCH_GATE_BASE` | a git sha or ref | task mode: the commit to diff against. full mode: the ship target ref, e.g. `origin/main`. May be unset. |

A repo gate that ignores both variables keeps its current behaviour.

## Who sets them

- **Daemon task gate** (`orchestrator/daemon.py` `_gate_worker`): `ORCH_GATE_MODE=task`,
  `ORCH_GATE_BASE=$(git merge-base goal/<parent> HEAD)`; without a parent the target `merge.merge` would use
  (`integration`). `gate.task_base` falls back to the target ref when merge-base fails but the ref resolves.
  When no base resolves the gate runs `full` (fail closed).
- **Merge gate** (`orchestrator/merge.py` `merge`, after the rebase): `ORCH_GATE_MODE=task`,
  `ORCH_GATE_BASE=<sha of the target branch>`. After the rebase, merge-base(target, HEAD) is the target tip.
- **Ship gate** (`orchestrator/ship.py` `_gate`): always `ORCH_GATE_MODE=full`, `ORCH_GATE_BASE=origin/<target>`.
  `[ship].gate_env` is merged over both and wins on a key clash.

`gate.run_gate(..., mode=, base=)` exports a variable only when its value is not None; the result dict and the
gate registry entry carry `mode` and `base`. `TESTS_GREEN_DRY=1` and the `tests-green: OK` line print
`mode=... base=...` so a dry run shows what the gate saw.

## Adopting it in a repo gate

1. If `ORCH_GATE_MODE=full`, run the full suite.
2. If `ORCH_GATE_BASE` is unset or does not resolve (`git rev-parse --verify "$ORCH_GATE_BASE^{commit}"` fails),
   run the full suite.
3. Otherwise select tests from `git diff --name-only "$(git merge-base HEAD "$ORCH_GATE_BASE")" HEAD`.
4. Any error while selecting falls back to the full suite. Never fail open to running nothing.

```bash
mode=${ORCH_GATE_MODE:-full}; base=${ORCH_GATE_BASE:-}
if [ "$mode" != task ] || [ -z "$base" ] || ! git rev-parse --verify -q "$base^{commit}" >/dev/null; then
  run_full; exit
fi
changed=$(git diff --name-only "$(git merge-base HEAD "$base")" HEAD) || { run_full; exit; }
run_selected "$changed"
```

### Example: luna

luna's `gate-scope.sh` diffs HEAD against `origin/main` today, so on a long-lived goal branch every task gate runs
the 40+ min suite. Adopting the contract:

- `LUNA_GATE_BASE` defaults to `ORCH_GATE_BASE` (`LUNA_GATE_BASE=${LUNA_GATE_BASE:-${ORCH_GATE_BASE:-origin/main}}`).
- `LUNA_GATE_FULL=1` when `ORCH_GATE_MODE=full`.

The luna change itself is a separate luna-inbox task; this repo only supplies the contract.

## Toggle

`.orchestrator/pool.toml`:

```toml
[gate]
scoped_task_gates = true  # default; revert: false
```

With `false` every gate path runs `ORCH_GATE_MODE=full` (today's behaviour, plus the variables).
