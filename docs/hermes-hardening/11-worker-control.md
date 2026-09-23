# Worker cancellation

`orchestrator workers cancel T-0001 --reason "Plan changed"` prints the partial
result as JSON. Existing `workers`, `--task`, `--all`, and `--json` inspection
commands retain their behavior.

`worker_control.cancel(task_id, reason, source="planner", grace_s=20)` records
`cancel_requested`, with status `cancelling`, source, and cancel_reason. Reasons
are nonempty single-line text of at most 512 characters; sources are registry
identifiers. Both Claude and Codex already record their subprocess PID at launch.
Cancellation uses that registry PID, sends SIGTERM, polls for up to the grace
period, and sends SIGKILL if it is still alive. Missing processes are harmless;
signal permission errors propagate, leaving the entry cancelling and capacity
reserved for operator recovery. The sleep, alive, and signal_fn parameters allow
instant deterministic tests. Unsafe process IDs are rejected before signalling.

The worktree, index, uncommitted files, branches, and commits are preserved.
Cancellation performs no checkout, reset, clean, commit, or worktree removal.
The partial result contains only observable evidence:

- `files_changed`: porcelain Git status paths, including untracked files and
  both paths of a rename.
- `diff_stat`: Git diff statistics against the merge-base with the goal branch,
  falling back to origin/main or main using the shared base resolver.
- `commits`: SHA and subject for commits since that base.
- `tests_run`: the last 20 lines of the newest task gate log under
  `.orchestrator/runs/tests`, or a worktree `tests-green.log` or `failures_only.log`.
- `errors`: the last 20 lines of an explicit registry exit-event `stderr_path`,
  if available.
- `files_inspected`: explicit `files_inspected` paths from registry tool events,
  if available.
- `unresolved`: an empty list; cancellation does not infer unfinished work.

Current registry producers retain neither stderr paths nor inspected-file paths,
so those fields normally remain empty. No provider conversation is parsed to
fill gaps. Diagnostic reads are bounded and text is filtered through the existing
credential redactor. Prompts, packets, transcripts, tool payloads, environment
variables, and hidden reasoning are never collected. Commit subjects and explicit
diagnostic artifacts are evidence supplied by the repository, not instructions.

The bus task becomes `held` with hold_reason `cancelled` and pid null. Its result
contains the partial, cancel_reason, cancelled_by, confidence 0.0, and provenance
`worker_partial`. Lists and diff statistics are truncated until the complete
result fits the bus's 6000-character cap. The returned partial matches the stored
partial. Pool.release frees the reservation, then the registry records cancelled
and its terminal exit. Repeating cancellation of a cancelled worker returns the
stored partial. Late registry finish calls cannot replace cancellation status.

The daemon skips cancelled holds during automatic fix rounds. Dead-worker
reconciliation checks the current registry, even when its bus snapshot is older,
and returns `cancelled` without bus updates for cancelling or cancelled entries.
The Planner owns any subsequent re-specification. This is an explicit control
operation; it introduces no adaptive routing or automatic cancellation policy.


## Steering

`orchestrator workers steer T-0001 --reason "New evidence" --message "Inspect the failing check"`
interrupts and resumes a recorded conversation. The call waits for the resumed
worker's result, using the normal role timeout. This explicit Planner control
introduces no adaptive policy or shadow-mode changes to worker prompts.

`worker_control.steer(task_id, message, reason=..., source="planner")` first
appends a durable row with ts, task, reason, message, and source to
`.orchestrator/workers/<task>.steering.jsonl`. Text is credential-redacted before
persistence and delivery. The registry's `steer` event contains only the message
character count and runtime metadata. The bus update records an event and
increments `packet_meta.steering_count`; the existing reason field is preserved.
The original spec, acceptance criteria, and scope are immutable: steering never
rewrites any part of that task contract.

The registry enters `steering`, advances its integer epoch (initially 1), and
stamps `pipeline.steer_epoch` before interrupting the PID. The same bounded
SIGTERM/grace/SIGKILL routine as cancellation runs, without holding the task or
releasing its reservation. Missing thread/session identifiers refuse before any
signal or steering write. Unsafe PIDs also refuse. Signal failures leave the
recorded steering state and reservation available for operator recovery.

Delivery is framed as `Steering from <source> (<reason>):`, the message, then
`The original task contract is unchanged.` Codex uses a dedicated
`executor.steer_resume` with `codex exec resume <thread>` and the resume access
flags; process cwd selects the worktree. It never calls reply or resume_plan
and never increments the fix-loop rounds counter. Claude uses
`claude -p --resume <session_id>` through the original role launch path, reusing
the recorded account, model, and effective tool allowlist, including any prior
disclosure escalation. Environment policy and read-only role restrictions apply.

The resumed process records `steered` and returns to `running` at launch;
normal completion then records its terminal status. Launch bookkeeping preserves
the advanced epoch. Old finish calls, late bus result/status writes, and capacity
releases are guarded by their captured launch epoch. The resumed run owns the
existing reservation and releases it when it finishes.
