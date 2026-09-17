You did not write this spec and will not implement it. Review the execute task spec below against the current code at its scoped paths BEFORE any code is written. Complexity {{complexity}}.
Spec: {{spec}}
Acceptance: {{acceptance}}
Scope: {{scope}}
Current code excerpts: {{code}}
Look for: wrong or missing insertion points, hidden coupling with files outside scope, acceptance criteria that cannot be verified, design choices that a reviewer would reject (state that never decreases, double counting, unguarded external calls, silent fallbacks), missing rollback.
Each excerpt line is prefixed "N| "; line = the numbered line in the excerpt, 0 if none.
Return ONLY JSON: {"verdict":"approve|request_changes","risks":[{"path":"","issue":"","severity":"low|med|high","line":0}],"suggested_spec_changes":[""]}
