# Goal containers

A goal task is marked by `constraints.goal: true`. It is a container for execute children, not a work item.

The daemon never dispatches, gates, merges, repairs, or dead-worker-reconciles a goal container. Executor and
worker entry points also refuse one with status `refused` and reason `goal container`. This prevents a container
result from closing the goal while child work is unfinished. If an old goal already has a dispatch stamp, the
daemon leaves it unchanged and reports the condition once per process.

Automatic closure belongs only to the Planner's `closable` decision. That decision becomes available when the
goal has execute children, every execute child has `merged_into` set, and no child remains queued or running.
The Planner may then post the goal result through its own bus path.

To revert this behavior, use `git revert` on the commit that introduced the goal-container guards, then run the
repository tests-green hook before deploying the daemon.
