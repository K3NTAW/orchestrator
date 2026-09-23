# Worker environment filtering (P22)

Measured 2026-09-23 in the T-1114 execution environment. The pool starts in
shadow mode. Off merges parent and caller environments without computing a
filter; shadow also reports what active would remove; active filters parent
keys and then overlays all caller extras. Extras always win. Missing policy
settings default to shadow and the documented default allowlist in env_policy.py.
Credential vetoes match whole underscore-separated tokens, case insensitively.
Exact allowlist names and role-secret names override that veto; globs do not.
Thus GIT_AUTHOR_NAME and the exact default SSH_AUTH_SOCK survive.

## Measured names only

The following lists were obtained by calling worker_env against os.environ in
shadow mode with the repository pool config. Logging was intercepted in memory
for this audit; no environment values were printed or saved.

Parent environment, execute role, no extras:

`AI_AGENT`, `CLAUDECODE`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CODEX_CI`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_VERSION`, `COLORTERM`, `COMMAND_MODE`, `COREPACK_ENABLE_AUTO_PIN`, `GH_PAGER`, `LC_CTYPE`, `LaunchInstanceID`, `NO_COLOR`, `NoDefaultCurrentDirectoryInExePath`, `OSLogRateLimit`, `PAGER`, `PWD`, `SECURITYSESSIONID`, `SHLVL`, `SSH_SOCKET_DIR`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `UV`, `WARP_CLIENT_VERSION`, `WARP_CLI_AGENT_PROTOCOL_VERSION`, `WARP_FOCUS_URL`, `WARP_HONOR_PS1`, `WARP_IS_LOCAL_SHELL_SESSION`, `WARP_PROMPT_NODE_VERSION_ENABLED`, `WARP_SSH_REUSE_CONTROL_MASTER`, `WARP_TERMINAL_SESSION_UUID`, `WARP_USE_SSH_WRAPPER`, `XPC_FLAGS`, `XPC_SERVICE_NAME`, `_`, `__CFBundleIdentifier`, `__CF_USER_TEXT_ENCODING`

Codex environment, with ORCH_TASK_ID and available HOME/CODEX_HOME extras:

`AI_AGENT`, `CLAUDECODE`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CODEX_CI`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_VERSION`, `COLORTERM`, `COMMAND_MODE`, `COREPACK_ENABLE_AUTO_PIN`, `GH_PAGER`, `LC_CTYPE`, `LaunchInstanceID`, `NO_COLOR`, `NoDefaultCurrentDirectoryInExePath`, `OSLogRateLimit`, `PAGER`, `PWD`, `SECURITYSESSIONID`, `SHLVL`, `SSH_SOCKET_DIR`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `UV`, `WARP_CLIENT_VERSION`, `WARP_CLI_AGENT_PROTOCOL_VERSION`, `WARP_FOCUS_URL`, `WARP_HONOR_PS1`, `WARP_IS_LOCAL_SHELL_SESSION`, `WARP_PROMPT_NODE_VERSION_ENABLED`, `WARP_SSH_REUSE_CONTROL_MASTER`, `WARP_TERMINAL_SESSION_UUID`, `WARP_USE_SSH_WRAPPER`, `XPC_FLAGS`, `XPC_SERVICE_NAME`, `_`, `__CFBundleIdentifier`, `__CF_USER_TEXT_ENCODING`

These are the keys active would strip on this machine, not a universal inventory.
The human should review this list before changing env_mode to active. No
secrets.execute table is configured here; when present its resolved keys are
also explicit extras. Existing secret resolution is unchanged.

## Launch coverage

- spawn.run_claude: role-specific Claude workers; extras include config directory,
  task ID, orchestrator root, resolved role secrets and the selected account's
  OAuth value under CLAUDE_CODE_OAUTH_TOKEN.
- executor._run: both codex exec and resume; execute role, task ID, resolved
  execute secrets, and HOME/CODEX_HOME when present for Codex authentication.
- goals._create_goal_task: Python goal-creation subprocess; target pool policy,
  ORCH_ROOT and ORCH_GOAL_TEXT extras, and a generated launch ID before a goal exists.
- goals.launch_planner: Claude Planner; target pool policy, config directory,
  ORCH_ROOT, account OAuth, existing filtered Planner secrets and caller extras.
  Callers may supply the goal ID; otherwise the policy uses a generated launch ID.
- planner_shadow.launch: Claude shadow Planner; pool policy, config directory,
  ORCH_ROOT, ORCH_SHADOW and account OAuth; no Planner service secrets are resolved.
  Uses a supplied task ID or the launch log's stem.

## Logging and existing redaction

Each shadow/active policy call attempts one runs row with role env_policy,
outcome, task/launch ID, sorted stripped names and count. Policy rows never carry
environment values, including allowed and caller-provided values. Logging failures
are swallowed without printing the exception. Off writes no policy row.

Goal creation, Planner launches and shadow Planner launches pass their target root
explicitly. When that resolved root differs from the process state root, policy
telemetry appends directly to the target's .orchestrator/runs/<date>.jsonl, with the
bus JSONL envelope (timestamp, attempt, context and event fields). It never calls
the home bus or performs its process-global task/config enrichment. Same-root
calls continue to use bus.log_run. No global state or ORCH_ROOT is rebound.
This guarantee concerns policy telemetry; it does not claim arbitrary subprocess
output is automatically sanitized.

jev.redact already sanitizes outbound state in jev.ask; gate input snippets and
target paths in jev_gate; persisted evidence content and summaries; ranking inputs
in jev_rank; routing, point, scheduler, Planner and skill advisory inputs in
jev_route, jev_points, jev_sched, jev_planner and jev_skills; registry bodies and
provenance in skills_registry; and shadow Planner launch errors/evaluation strings.
It recognizes credential-like text and long encoded strings. Environment policy
logging does not rely on that heuristic: it never includes values in the first place.
