# External skill inspection risk

Quarantined external skills are inspected as inert data. Their existing `findings.json` report contains a top-level `risk` object covering scripts, endpoints, declared or mentioned tools, credential-reference names, dependencies, filesystem-sensitive content, and the per-file context scan.

Risk is `high` when any context scan is blocked, endpoints and credential references coexist, recursive deletion or permission changes appear, or a dependency uses a URL or Git source. It is `medium` when scripts, endpoints, dependencies, or credential references are present, and `low` otherwise.

Every path into testing uses the same gate. The report must exist and match both the registered and current quarantine content hash. Existing block-severity and script controls remain in force, and high risk or a blocked overall scan adds a block. Reasons beginning with `override:` or `human-reviewed:` bypass these content controls after a valid matching inspection; the full reason is retained in lifecycle history. Rollback reuses the original quarantined-to-testing approval reason.

`orchestrator skills inspect ID` prints the complete persisted report. `orchestrator skills list` adds its risk level, or `-` when no quarantine report exists.
