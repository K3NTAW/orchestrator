---
name: challenge
description: Try to refute another scout's low-confidence claim with repo evidence. Use for challenge tasks.
---
# Challenge
Read-only; a human reviews merges. Assume the claim is wrong and look for counter-evidence: a contradicting call site, a test proving other behavior, an overriding config. None found in ≤10 tool calls → `confirmed`; mixed → `uncertain`. Return ONLY `{"verdict":"confirmed|refuted|uncertain","evidence":["path:line"],"note":""}`.
