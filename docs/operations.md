# Operations

## Gotchas

- launchd soft open-file limit 256 breaks gates. `launchctl limit maxfiles` reports a soft limit of 256 for
  LaunchAgents, so a daemon restarted by the watchdog (com.orchestrator.watchdog) and every gate it spawns
  inherit 256 and fail with `OSError: [Errno 24] Too many open files`. `orchestrator daemon` and
  `orchestrator watchdog` now raise the soft `RLIMIT_NOFILE` at start to min(hard, `[daemon].nofile_soft`)
  (default 65536, `orchestrator/machine.py` `raise_nofile`); gates and workers inherit it. A higher soft limit
  is never lowered. Set `nofile_soft = 0` to leave the limit alone. The LaunchAgent plist is protected, so the
  fix lives in code.
