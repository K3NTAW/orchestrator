# Context-program modes

The context program uses `off`, `shadow`, and `active` feature modes. All three
switches default to `shadow`; set a switch's `mode` back to `off` to revert it.

## Switches

- `[context_router]`: shadow selects and logs a routed context packet, while the
  model continues to receive the unchanged packet.
- `[tool_disclosure]`: shadow selects and logs which tools would be disclosed,
  without changing the tools shown to the model.
- `[instructions]`: shadow selects and logs conditional instruction modules,
  without changing loaded instructions. Fundamental safety rules are never
  conditional.

## Decision-log vocabulary

Selection logs `context_selection`; reuse and retrieval log `evidence_reuse` and
`retrieval`; disclosure logs `tool_disclosure`; instruction assembly logs
`instruction_loading`; routing logs `model_routing` and `planner_routing`;
scheduling logs `scheduling`; execution authorization logs `action_gate`;
workflow planning logs `workflow_strategy`; and transfer logs `handoff`.

Legacy call sites keep `routing`, `wave`/`jev_sched`, `strategy`, and
`planner_route`. New context-program code uses their generic counterparts
`model_routing`, `scheduling`, `workflow_strategy`, and `planner_routing` so a
single decision is not logged twice.

Promotion remains evidence-gated. P27/P28 scorecards will supply evidence; until
then all three features remain in shadow with an `insufficient_evidence` result.
