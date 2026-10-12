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

## Waiting registry entry

Added 2026-10-11 (T-1852, part B1 of T-1792 item 2). A gate queued for the machine heavy-gate slot keeps
`<STATE>/gates/<key>.json` while it waits. `gate.write_waiting_entry(key, *, task_id, worktree, mode, base)` writes:

```json
{"state": "waiting", "task_id": "T-1", "worktree": "/path", "owner_pid": 123, "owner_start": "...",
 "waiting_for": "machine_slot", "waiting_since": 1800000000.0, "mode": "task", "base": "<sha>"}
```

`owner_*` come from `gate._owner()`; `owner_start` may be null. The entry has no `pid`, `pgid`, `proc_start`,
`started_at` or `timeout_s`: no gate process exists yet. Since part B2a (2026-10-12) `run_gate` writes it while a
heavy gate queues; see [Machine-wide heavy gate slot](#machine-wide-heavy-gate-slot).

Every registry write goes through `gate._write_registry`: write `<key>.tmp`, then `os.replace`. `register()` in
`_run_gate` uses it too, so it can overwrite a waiting entry in place with no moment where the key is in no
registry. Readers glob `*.json`, so a `.tmp` file is never read.

Predicates:

- `gate.is_waiting(entry)`: `entry` is a dict and `entry["state"] == "waiting"`.
- `gate._owner_alive(entry)`: `owner_pid` is an int, `machine._pid_alive(owner_pid)`, and `owner_start` is null or
  `machine.process_start(owner_pid)` is null or equals it. Never calls `_group_alive`.

Every reader branches on a waiting entry before it touches `entry["pid"]`:

| Reader | Waiting entry | Behaviour change |
|---|---|---|
| `gate._live_registry_keys` | counted when `_owner_alive`, else skipped | new: a queued heavy gate keeps the per-repo `[gate].max_parallel` slot it already took; other processes see it here. Intended. |
| `gate.running_gates` | skipped (not nearing a timeout; it has none) | none, was skipped through the missing `started_at`; now explicit |
| `gate.reap_orphans` | never signals; unlinked when not `_owner_alive` (dead owner, reused pid, or a stale entry with this process's pid but another start); kept when the owner lives | new: such an entry used to be skipped on `KeyError` and leaked |
| `gate.terminate_own` | unlinked when `owner_pid` is this process | none (already did so) |
| `watchdog.gate_live` | not live | none, was not live through the missing `pid`; now explicit |

## Machine-wide heavy gate slot

Added 2026-10-12 (part B2a of T-1792 item 2). At most `[gate].machine_max_parallel` heavy gates run at once across
every repo on the machine. `run_gate` takes the per-repo `[gate].max_parallel` slot first (unchanged), then, before
the first attempt, a heavy gate queues for a machine lease of kind `heavy_gate` in `~/.orchestrator-machine/`.

- Heavy: `gate._heavy(mode, opts)` is `mode in (None, "full") or [gate].heavy`. Mode `task` with `heavy = false`
  is light and never touches the lease. Ship and unscoped gates are always heavy.
- Holder: `{"repo": str(STATE.parent), "task": task_id, "pid": os.getpid(), "proc_start": <owner start>}`
  (`proc_start` dropped when unreadable); `machine.acquire` adds `since`.
- FIFO: the gate writes its [waiting registry entry](#waiting-registry-entry), takes a ticket
  (`machine.wait_ticket`, kept in `waiters-heavy_gate.json`) and polls `machine.acquire(..., ticket=ticket)` every
  `SLOT_POLL_S`; only the oldest live ticket is granted. On success `register()` overwrites the waiting entry in
  place with the running entry, so the key is never absent while the lease is held. Every other exit drops the
  ticket and unlinks the waiting entry when it is this process's waiting entry (a running entry is never deleted).
- Queue time never counts toward `[gate].timeout_s`. One lease wraps both timeout attempts (no re-wait) and is
  released in one `finally`; a release error is logged and never masks the result.
- Head-of-line: a queued gate keeps its per-repo slot while it waits. That wait is bounded by
  `machine_wait_max_s`, default `2 * (timeout_s + 2 * cleanup_timeout_s) + 60` (4500/300 gives 10260 s): two
  attempts, each followed by `cleanup_cmd` and `post_cmd` bounded by `cleanup_timeout_s`, plus kill grace, so one
  queued gate always outlives one worst-case holder.
- Ticket re-issue: when the gate's ticket disappears from `waiters-heavy_gate.json` (an operator cleared it), the
  gate takes a new ticket at the tail, at most `machine_ticket_reissue_max` times, logging
  `machine slot ticket vanished, re-queued at the tail (n/max)`. The wait clock is never reset.
- Log: `[gate] task_id=... waiting for machine heavy-gate slot (cap=N, holders=[(repo, task, age_s), ...])` at the
  first miss and every `machine_wait_log_s`.
- Every result carries `machine_wait_s` (float, 0.0 for light gates and cap 0) and `machine_wait_exceeded` (bool).

Failure shapes (both leave the repo slot released by `run_gate`'s `finally`):

| Case | Result | Current consumers |
|---|---|---|
| `acquire` / `wait_ticket` raise (OSError, ValueError, JSON errors) | `returncode: 1`, `stderr` and `error` `machine slot: <exc!r>`, `timed_out: false`, `attempts: 0`, `timeouts: 0`, `machine_wait_exceeded: false` | red in `daemon._apply_gate_result`, `merge.merge`, `ship._gate` |
| wait exceeded, or the ticket vanished more than `machine_ticket_reissue_max` times | `returncode: null`, `timed_out: true`, `stderr` `machine slot wait exceeded <n>s` or `machine slot ticket vanished <n> times`, `attempts: 0`, `timeouts: 0`, `machine_wait_exceeded: true` | a plain gate timeout with no retry (the daemon holds `gate_timeout`; `timeouts: 0` leaves `pipeline.gate_timeouts` untouched, so `auto_fix_round` does not fire) until T-1797 |

Keys (`[gate]`, read by `gate.settings()`, never written back). An invalid value is logged to stderr as
`[gate] invalid [gate].<key>=<value!r>, using default <default>` and replaced by the default, never by 0 or
unlimited. A bool is never a valid int.

| Key | Default | Valid |
|---|---|---|
| `machine_max_parallel` | 1 | int >= 0; 0 = off (takes no lease) |
| `heavy` | false | bool |
| `machine_wait_max_s` | `2 * (timeout_s + 2 * cleanup_timeout_s) + 60` | int >= 1 |
| `machine_wait_log_s` | 300 | int >= 1 |
| `machine_ticket_reissue_max` | 3 | int >= 0 |

To raise the cap, set `machine_max_parallel` in the `pool.toml` of every repo on the machine; each gate passes its
own repo's cap to `acquire`, so keep them equal.

Daemon shutdown aborts its own queued waiters: `terminate_own` kills running gate process groups and marks every
gate thread of its STATE queued for the machine slot, which raises `Aborted` at its next check (before each
acquire, after each sleep, and right after acquire, releasing the lease it just took) with its ticket dropped,
waiting entry unlinked and repo slot released. Waiters of another STATE or another process keep their place. A
daemon gate thread is a daemon thread, so if the daemon exits first it dies with the process; its ticket and
waiting entry are then swept by pid liveness
(`machine` drops dead waiters, `reap_orphans` unlinks waiting entries of a dead owner).

### Rollback and stuck leases

Set `machine_max_parallel = 0` to turn the slot off: gates take no lease and never queue (tested). To clear a
stuck lease, delete the `heavy_gate` entries from `~/.orchestrator-machine/leases.json` and
`waiters-heavy_gate.json` (queued gates re-issue their tickets, at most `machine_ticket_reissue_max` times), and
the `.orchestrator/gates/<key>.json` waiting entries whose owner is dead. The abort marks (`gate._SLOT_WAITERS`) are in memory only and popped on
every waiter exit, so there is nothing to clean on disk. A live holder is bounded by the wait
formula above; a hung daemon thread keeps its lease until the daemon exits. Lease liveness is pid-based: when a
daemon dies while its gate subprocess still runs, the lease is swept and the cap can be exceeded by one until
`reap_orphans` kills the orphan.
