"""Prompt-cache economics derived from canonical run rows."""
import json
import time
from collections import defaultdict
from pathlib import Path

from . import bus


DEFAULT_RATIOS = {
    "claude_read_ratio": 0.1,
    "claude_write_ratio": 1.25,
    "codex_read_ratio": 0.25,
    "codex_write_ratio": 1.0,
    "output_ratio": 5.0,
}


def normalize(provider, usage):
    """Return the canonical cache buckets plus a zero-safe hit ratio."""
    values = bus.normalize_usage(provider, usage)
    keys = ("input_uncached_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    out = {key: values[key] for key in keys}
    denominator = sum(out[key] for key in keys[:3])
    out["hit_ratio"] = out["cache_read_tokens"] / denominator if denominator else 0.0
    return out


def _cache_cfg(cfg):
    values = dict(DEFAULT_RATIOS)
    values.update((cfg or {}).get("cache") or {})
    return values


def effective_cost(norm, cfg):
    """Return effective token-equivalents using provider-specific cache ratios."""
    ratios = _cache_cfg(cfg)
    provider = norm.get("provider", "claude")
    return (norm.get("input_uncached_tokens", 0)
            + norm.get("cache_read_tokens", 0) * ratios[f"{provider}_read_ratio"]
            + norm.get("cache_write_tokens", 0) * ratios[f"{provider}_write_ratio"]
            + norm.get("output_tokens", 0) * ratios["output_ratio"])


def annotate(row, cfg=None):
    """Add derived fields to a normalized run row without renaming token buckets."""
    denominator = sum(row.get(key, 0) or 0 for key in
                      ("input_uncached_tokens", "cache_read_tokens", "cache_write_tokens"))
    row["hit_ratio"] = (row.get("cache_read_tokens", 0) or 0) / denominator if denominator else 0.0
    values = {**row, "provider": row.get("provider") or "claude"}
    row["effective_tokens"] = effective_cost(values, cfg if cfg is not None else bus.pool_config())
    meta = row.get("packet_meta")
    prefix = row.get("prefix_sha")
    if prefix is None and isinstance(meta, dict):
        prefix = meta.get("prefix_sha")
    if prefix is None and isinstance(row.get("context"), dict):
        prefix = row["context"].get("prefix_sha")
    row["prefix_sha"] = prefix
    return row


def _pool_cfg(root):
    import tomllib
    root = Path(root)
    candidates = (root / "pool.toml", root / ".orchestrator" / "pool.toml")
    for path in candidates:
        try:
            return tomllib.loads(path.read_text())
        except FileNotFoundError:
            continue
    return {}


def _summary(rows):
    total = {"runs": len(rows)}
    for key in ("input_uncached_tokens", "cache_read_tokens", "cache_write_tokens"):
        total[key.removesuffix("_tokens")] = sum(row.get(key, 0) or 0 for row in rows)
    denominator = total["input_uncached"] + total["cache_read"] + total["cache_write"]
    total["hit_ratio"] = total["cache_read"] / denominator if denominator else 0.0
    total["effective_tokens"] = sum(row.get("effective_tokens", 0) or 0 for row in rows)
    known = [row.get("prefix_sha") for row in rows if row.get("prefix_sha")]
    total["distinct_prefix_sha"] = len(set(known))
    total["unknown_prefix_runs"] = len(rows) - len(known)
    seen, repeats = set(), 0
    for prefix in known:
        if prefix in seen:
            repeats += 1
        seen.add(prefix)
    total["reuse_rate"] = repeats / len(known) if known else None
    return total


def report(root, days=7):
    """Aggregate recent JSONL run rows by role, provider, and goal."""
    root = Path(root)
    runs = root / "runs" if (root / "runs").exists() else root / ".orchestrator" / "runs"
    cutoff = time.time() - days * 86400
    cfg = _pool_cfg(root)
    rows = []
    for path in sorted(runs.glob("*.jsonl")) if runs.exists() else ():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get("ts", 0) < cutoff or "role" not in row:
                continue
            try:
                annotate(row, cfg)
            except Exception:
                continue
            rows.append(row)
    def grouped(key):
        groups = defaultdict(list)
        for row in rows:
            groups[row.get(key) or "unknown"].append(row)
        return {name: _summary(values) for name, values in sorted(groups.items())}
    return {"days": days, "totals": _summary(rows), "by_role": grouped("role"),
            "by_provider": grouped("provider"), "by_goal": grouped("goal_id")}


def format_report(card):
    lines = ["group\truns\tuncached\tread\twrite\thit_ratio\teffective\tprefixes\treuse_rate"]
    for dimension in ("by_role", "by_provider", "by_goal"):
        for name, row in card[dimension].items():
            reuse = "unknown" if row["reuse_rate"] is None else f"{row['reuse_rate']:.3f}"
            lines.append(f"{dimension[3:]}:{name}\t{row['runs']}\t{row['input_uncached']}\t"
                         f"{row['cache_read']}\t{row['cache_write']}\t{row['hit_ratio']:.3f}\t"
                         f"{row['effective_tokens']:.1f}\t{row['distinct_prefix_sha']}\t{reuse}")
    return "\n".join(lines)
