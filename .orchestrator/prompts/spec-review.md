{{packet}}
You did not write this spec and will not implement it. Review it against the scoped repository shape BEFORE code is written.
Look for: wrong or missing insertion points, hidden coupling with files outside scope, acceptance criteria that cannot be verified, design choices that a reviewer would reject (state that never decreases, double counting, unguarded external calls, silent fallbacks), missing rollback.
Return ONLY JSON: {"verdict":"approve|request_changes","risks":[{"path":"","issue":"","severity":"low|med|high","line":0}],"suggested_spec_changes":[""]}
