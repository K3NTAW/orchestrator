"""Deterministic P34 checks for HOT, WARM and COLD memory behavior."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import memory_hot, memory_store


def _case(name, passed, detail):
    return {"name": name, "passed": bool(passed), "detail": detail}


def _seed(root):
    state = root / ".orchestrator"
    memory = state / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (state / "pool.toml").write_text("[memory]\nhot_budget_tokens=120\nhot_recent_days=30\nhot_never_compact=[]\n")
    records = [
        memory_store.Record(id="current-routing", kind="decision", title="Current routing choice",
                            date="2099-01-01", components=["router"], files=["orchestrator/router.py"],
                            tier="warm", body="Current routing choice for router."),
        memory_store.Record(id="older-index", kind="decision", title="Older lunar indexing decision",
                            date="2020-01-01", components=["indexer"], tier="warm",
                            body="The lunar index remains searchable."),
        memory_store.Record(id="historical-proof", kind="decision", title="Historical provenance choice",
                            date="2019-01-01", tier="cold", superseded_by="T-9000",
                            body="Superseded provenance remains available."),
        memory_store.Record(id="irrelevant-painter", kind="decision", title="Unrelated painter palette",
                            date="2021-01-01", components=["painter"], tier="warm",
                            body="Palette facts unrelated to routing."),
    ]
    for record in records:
        memory_store.add(record, root)
    return records


def run(root=None):
    temporary = None
    if root is None:
        temporary = tempfile.TemporaryDirectory(prefix="orchestrator-memory-eval-")
        root = Path(temporary.name)
    else:
        root = Path(root)
    records = _seed(root)
    try:
        compacted = memory_hot.compact(root)
        hot = (root / ".orchestrator/memory/HOT.md").read_text()
        cases = [_case("current_decision_in_hot", "current-routing" in {r["id"] for r in compacted["records"]}
                       and "Current routing choice" in hot, "current governing decision is rendered in HOT")]
        memory_store.set_tiers({"older-index": "warm", "historical-proof": "cold", "irrelevant-painter": "warm"}, root)
        older = memory_store.search("lunar", tier="warm", root=root)["records"]
        cases.append(_case("older_decision_in_warm_search", any(r["id"] == "older-index" for r in older),
                           "older decision is returned by WARM full-text search"))
        historical = memory_store.search("provenance", tier="cold", root=root)["records"]
        cases.append(_case("historical_provenance_in_cold_search",
                           any(r["id"] == "historical-proof" and r["tier"] == "cold" for r in historical),
                           "superseded provenance is returned with tier cold"))
        relevant = memory_store.search("router", tier="hot", root=root)["records"] + memory_store.search(
            "router", tier="warm", root=root)["records"]
        cases.append(_case("irrelevant_record_excluded", all(r["id"] != "irrelevant-painter" for r in relevant),
                           "task query excludes the unrelated record from tiered sections"))
        memory_hot.compact(root)
        findable = all(memory_store.get(record.id, root) is not None for record in records)
        cases.append(_case("compaction_preserves_knowledge", findable,
                           "every seeded record remains findable after compaction"))
        return {"cases": cases, "passed": sum(case["passed"] for case in cases), "total": len(cases)}
    finally:
        if temporary is not None:
            temporary.cleanup()


def format_report(result):
    lines = [f"{case['name']}\t{'PASS' if case['passed'] else 'FAIL'}\t{case['detail']}" for case in result["cases"]]
    lines.append(f"passed={result['passed']}/{result['total']}")
    return "\n".join(lines)
