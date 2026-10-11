"""Shared hermetic fixtures for tests that must not see the checkout's live operator settings.

tests/_harness.py builds the shared TMP root's pool.toml from the checkout's own .orchestrator/pool.toml. That file
carries live operator settings (2026-10-09, PR 50: every Codex executor disabled, budgets and routing modes raised),
so any test that asserts dispatch behaviour against "the committed pool.toml" changes outcome with whatever the
operator last committed. BASELINE_POOL_TOML pins the config those tests were written against: the sanitized
pool.toml the harness produced at 569f4b1 (comments stripped; the documented copy is .orchestrator/pool.toml).
A test module that depends on pool settings calls pin_pool() in setUpModule and the returned restore in
tearDownModule, so every reader of STATE / "pool.toml" (pool.config, allocation, watchdog, promotion, bus) sees the
baseline for that module only and later modules see the harness file unchanged."""
from pathlib import Path

from _harness import TMP

BASELINE_POOL_TOML = r'''
window_cap_tokens = 10_000_000

[cache]
claude_read_ratio = 0.1
claude_write_ratio = 1.25
codex_read_ratio = 0.25
codex_write_ratio = 1.0
output_ratio = 5.0

[[claude_accounts]]
id = "A"
config_dir = "~/.claude-a"
role_affinity = ["planner", "scout", "triage", "execute", "review", "spec_review"]
reserve_for_planner = 0.35
daily_budget_tokens = 30_000_000

[[claude_accounts]]
id = "B"
config_dir = "~/.claude-b"
role_affinity = ["scout", "triage", "review", "challenge", "execute", "spec_review"]
reserve_for_planner = 0.0
daily_budget_tokens = 40_000_000

[daemon]
autostart = true
interval_s = 30
respawn_after_s = 120
respawn_max = 3
stage_lease_s = 900
auto_fix_rounds = 2
close_retry_s = 900
close_max_attempts = 3
flaky_rerun_max = 1
flaky_rerun_timeout_s = 600

[gate]
timeout_s = 2700
cleanup_timeout_s = 300

[ship]
enabled = false
target = "main"
max_regates = 3

[planner]
autonomous = false
handover_context_tokens = 150000
decision_packet_chars = 6000

[planner.routes]
enabled = true
auto_open_pr = false
escalate_tier = "fable"
investigate_tier = "sonnet"
investigate_max_complexity = 4
premium_launches_soft_per_goal = 2

[planner.routing]
mode = "shadow"
default_tier = "opus"
escalation_tier = "fable"
hard_complexity_min = 9
hard_security = true
hard_spec_review_request_changes_min = 2
hard_ambiguous = true
min_samples = 20
shadow_sample_rate = 1.0
fable_reserve_share = 0.2
jev_mode = "shadow"
soft_threshold = 0.5

[review]
complementary = false
spec_review_min = 6
direct_merge_max = 3
two_reviews_from = 7
spec_review_tier = "sonnet"
code_review = "security_paths"
security_review_tier = "sonnet"
security_paths = [
    "orchestrator/*.py",
    ".claude/hooks/**", ".claude/settings.json", ".orchestrator/pool.toml", ".orchestrator/protected-paths.txt",
    ".orchestrator/prompts/**", "skills/**", ".claude/skills/**",
    ".mcp*.json", ".mcp.worker.json", "Dockerfile", "docker-compose*.yml", "pyproject.toml", "uv.lock",
]
semantic_paths = [
    "pyproject.toml", "uv.lock", "Dockerfile*", "docker/**", "**/migrations/**",
    "orchestrator/serve.py", "orchestrator/mcp.py", "orchestrator/bus_mcp.py", "orchestrator/cli.py",
]
semantic_patterns = { authorization = "(?i)\\b(?:bearer|token|authenticate|authorize|permission)\\b", database = "(?i)\\b(?:ALTER TABLE|CREATE TABLE)\\b", exports = "^\\s*__all__\\s*=", mcp_or_cli = "@mcp\\.tool|add_parser\\(" }

[review.areas.auth]
paths = ["**/auth*", "auth*", "**/permissions*"]
patterns = ['(?i)\b(?:bearer|authenticate|authorize|permission)\b']
[review.areas.migrations]
paths = ["**/migrations/**", "migrations/**"]
patterns = ['(?i)\b(?:ALTER|CREATE|DROP) TABLE\b']
[review.areas.interfaces]
paths = ["**/api/**", "orchestrator/mcp.py", "orchestrator/cli.py"]
patterns = ['^\s*__all__\s*=', '@mcp\.tool|add_parser\(']
[review.areas.data_deletion]
paths = []
patterns = ['(?i)\b(?:DELETE FROM|TRUNCATE|DROP TABLE)\b', '\b(?:rmtree|unlink)\(']

[codex]
home = "~/.codex"
model = "gpt-6-astra"
max_parallel = 2
daily_budget_tasks = 40
on_exhausted = "fallback_claude"
dangerous_full_access = true

[[executors]]
id = "astra"
provider = "codex"
model = "gpt-6-astra"
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = true

[[executors]]
id = "luna"
provider = "codex"
model = "gpt-5.6-luna"
roles = ["execute"]
complexity_min = 1
complexity_max = 6
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = true

[[executors]]
id = "terra"
provider = "codex"
model = "gpt-5.6-terra"
roles = ["execute"]
complexity_min = 1
complexity_max = 6
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = true

[[executors]]
id = "sol"
provider = "codex"
model = "gpt-5.6-sol"
roles = ["execute"]
complexity_min = 1
complexity_max = 6
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = true

[[executors]]
id = "luna6"
provider = "codex"
model = "gpt-6-luna"
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = false

[[executors]]
id = "terra6"
provider = "codex"
model = "gpt-6-terra"
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = false

[[executors]]
id = "sol6"
provider = "codex"
model = "gpt-6-sol"
roles = ["execute"]
complexity_min = 1
complexity_max = 10
max_parallel = 2
daily_budget_tasks = 40
quota_group = "chatgpt"
weight = 1.0
enabled = false

[models]
rank_by = "success"
success_floor = 0.6
min_samples = 3
haiku   = "claude-haiku-4-5-20251001"
sonnet  = "claude-sonnet-5-5"
opus    = "claude-opus-5-5"
planner = "claude-fable-5-1"

[limits]
reservations = true
default_tokens_per_usd = 250000
max_parallel_claude_workers = 4
cooldown_default_s = 1800
max_turns = { triage = 5, scout = 12, review = 10, challenge = 15, execute = 40, planner = 400 }
max_budget_usd = { triage = 0.5, scout = 1.0, review = 3.0, challenge = 1.5, execute = 6.0, planner = 10.0, planner_decision = 3.0, planner_shadow = 1.5 }
timeout_s = { triage = 300, scout = 600, review = 900, challenge = 900, execute = 1800, planner = 7200 }
review_diff_chars = 12000
spec_review_code_chars = 8000
recall_hits = 30

[memory]
mode = "shadow"
packet_hot_tokens = 1200
budget_hits = 8
min_score = 0.5
budget_chars = 6000
packet_layers = ["notes", "bus"]

hot_budget_tokens = 3000
hot_never_compact = ["gotcha:open", "architecture", "decision:constraint", "decision:security"]
hot_recent_days = 14

[jev]
max_data_class = "INTERNAL"
votes = 1
block_redundant_p = 0.85
block_needed_p = 0.15
block_redundant_p_noconf = 0.92
block_needed_p_noconf = 0.08
enabled = true
model = "jev-latest"
timeout_s = 5
daily_budget_tokens = 20_000_000
max_state_chars = 100_000
gate_mode = "sample"
sample_rate = 0.1
block_repeats = false
read_suppression = "shadow"
max_suppressions_per_session = 20
max_false_suppression = 0.02

[jev.routing]
mode = "shadow"
cache_ttl_s = 86400
cache_max_entries = 500
max_adjustment = 0.25
evidence_floor_n = 10

[jev.points]
scout_necessity = "shadow"
context_escalation = "shadow"
review_escalation = "shadow"
planner_relaunch = "shadow"

[allocation]
mode = "shadow"
min_samples = 5
latency_weight_usd_per_hour = 2.0
critical_factor = 1.0
non_critical_factor = 0.25

[strategy]
mode = "shadow"
min_samples = 10

[speculation]
mode = "off"
min_fix_round_p = 0.5
min_samples = 5
min_retry_cost_usd = 1.0

[context_router]
mode = "shadow"
cache_mode = "shadow"
legacy_evidence_chars = 6000

[tool_disclosure]
cache_mode = "shadow"
mode = "shadow"

[instructions]
mode = "active"

[promotion]
min_samples = 20

[handoff]
mode = "shadow"
min_gain = 0.15
max_active_share = 0.5

[secrets]
env_mode = "shadow"
env_passthrough = ["PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR", "XDG_*", "CLAUDE_*", "ORCH_*", "UV_*", "VIRTUAL_ENV", "PYTHONPATH", "NODE_*", "GIT_*", "SSH_AUTH_SOCK"]

[secrets.scout]
GH_READ_TOKEN = "source ~/.config/f/f.sh && f tok get gh-read --reveal"
[secrets.review]
GH_READ_TOKEN = "source ~/.config/f/f.sh && f tok get gh-read --reveal"
[secrets.triage]
[secrets.planner]
GH_WRITE_TOKEN = "env:GH_WRITE_TOKEN"
[secrets.jev]
TYPESAFE_API_KEY = "source ~/.config/f/f.sh && f tok get typesafe --reveal"

[scheduler]
mode = "shadow"
duration_mode = "empirical"
duration_min_samples = 5
duration_trim = 0.1
reserve_imminent = true
concurrency_mode = "adaptive"
merge_pressure_mode = "observe"
merge_queue_elevated = 3
merge_queue_saturated = 5
merge_conflicts_saturated = 2
jev_mode = "shadow"
jev_max_pairs = 8
jev_cache_ttl_s = 3600
jev_cache_max_entries = 200
soft_conflict_policy = "defer"
max_wave = 0
stale_rebase = false

[skills]
cache_mode = "shadow"
mode = "shadow"
jev_mode = "shadow"
jev_max_batch = 8
max_selected = 3
max_recovery = 0.10

[jev.skills]
cache_ttl_s = 86400
cache_max_entries = 500
trusted_repos = []
allowed_domains = []

[harness]
depth_mode = "shadow"

[contracts]
mode = "shadow"
repair_budget_usd = 0.5

[steering]
noncritical_medium_continue = true
mode = "shadow"
stuck_after_s = 900
out_of_scope_events = 3
min_interval_s = 1800
'''.lstrip("\n")


def pin_pool(state=None):
    """Write BASELINE_POOL_TOML over <state>/pool.toml (default: the shared TMP root) and return a callable that
    restores the previous file exactly."""
    path = Path(state or TMP / ".orchestrator") / "pool.toml"
    previous = path.read_text() if path.exists() else None
    path.write_text(BASELINE_POOL_TOML)

    def restore():
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous)
    return restore
