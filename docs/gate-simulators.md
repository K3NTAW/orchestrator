# Gate cleanup and iOS simulators

Booted simulators keep dozens of runtime processes alive. On a 16 GB Mac that pushed swap to 13 GB and made
every gate time out (2026-10-08). Two mechanisms shut them down once no test needs them.

## [gate].post_cmd

Runs after every gate attempt: green, red or timeout. Bounded by `[gate].cleanup_timeout_s`, cwd is the
worktree. A failure or timeout is logged to stderr and never changes the gate result.
`[gate].cleanup_cmd` still runs only after a timeout, before `post_cmd`.

Default for iOS repos, in the repo's `pool.toml`:

```toml
[gate]
post_cmd = "pgrep -f 'xcodebuild test' >/dev/null || xcrun simctl shutdown all"
```

## Watchdog sweep

Each watchdog pass (every 5 min via launchd) runs `xcrun simctl shutdown all` when simulators are booted and
nothing needs them. It skips when:

- any process runs `xcodebuild test`, `xcodebuild build-for-testing` or `simctl launch`
- a gate registry entry (`.orchestrator/gates/*.json`) in any watched repo names a live pid

It logs `[watchdog] shut down N idle simulators`. `simctl` only addresses simulators, never physical devices.
