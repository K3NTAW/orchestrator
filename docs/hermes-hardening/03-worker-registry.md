# Worker runtime registry

The registry stores one current snapshot per task in
`.orchestrator/workers/<task>.json` and append-only observations in
`.orchestrator/workers/<task>.events.jsonl`. It uses the existing reentrant bus
lock for mutations, fsyncs writes, and replaces snapshots atomically through a
temporary file in the same directory. The event append and snapshot replacement
are individually durable; they are not a single filesystem transaction. After a
crash between them, the last event can be newer than the snapshot.

Snapshots contain `task`, `role`, `model`, `provider`, `account`, `pid`,
`worktree`, `branch`, `started_at`, `last_event_at`, `stage`, `tokens`, `usd`,
`parent`, `children`, `status`, and `status_reason`. Unknown values are null;
unknown token buckets are omitted. Times are Unix seconds. Reads compute
`elapsed_s` from launch to now, or to the last event for a finished attempt.
`current_tool` is absent unless a tool event reports it. Codex's parsed thread
identifier is recorded separately as `thread`; `pid` is always the OS process
identifier, available before waiting for completion.

`parent` identifies the goal container. Children are task IDs whose
`constraints.fix_round_for` equals this worker's task ID. A task's `parent`
never establishes a worker-child relationship. Reads refresh children from task
files without changing task state.

Token buckets are `input_uncached`, `cache_read`, and `output`. Codex cached
input is subtracted from its inclusive input count. Claude cost is reported;
Codex cost is estimated using the pool's configured rates. Unknown cost is null.
Usage is recorded when the buffered provider result becomes available; the
registry does not infer intermediate progress or tools from result prose.

Each event has exactly `ts`, `kind`, and `data`. Supported kinds are `spawned`,
`claimed`, `thread`, `stage`, `tool`, `usage`, `exit`, `reconciled`, and `held`.
Event data uses the same restricted snapshot fields. Statuses are `starting`,
`running`, `waiting`, `steering`, `cancelling`, `cancelled`, `done`, `failed`,
and `held`. Held entries are inactive. A new starting attempt resets the current
snapshot while retaining the task's full event history.

`upsert`, `get`, `active`, `event`, and `finish` manage runtime observations.
`reconcile(alive_fn)` requires a caller-provided liveness function and never
imports the daemon. It marks starting or running entries with dead PIDs failed,
sets `status_reason` to `process_dead`, and emits `reconciled`. Entries without
a PID are left alone. The daemon invokes this once per tick after its dead-task
loop. Only `daemon.reconcile_dead` changes the corresponding task status.
Timeouts kill and reap the process before recording a failed exit.

`orchestrator workers` displays active workers with task, role, model, provider,
status, stage, elapsed time, known token total, and worktree. `--json` returns
snapshot objects. `--all` additionally includes inactive snapshots whose last
event is within 24 hours. `--task T-0001` returns the full snapshot plus its last
20 events, including older finished attempts. Inspection does not reconcile or
update snapshots.

The registry deliberately excludes packets, prompts, transcripts, result text,
tool arguments and outputs, environment variables, credentials, and hidden
reasoning. Only runtime identifiers, numeric counters, event kinds, and short
status reason codes belong here. Unknown fields and nonnumeric token data raise
`ValueError`; producers select counters and identifiers rather than copying
provider payloads. Errors are represented by codes such as `timeout`,
`process_error`, or `usage_limit`, never exception or stderr text.
