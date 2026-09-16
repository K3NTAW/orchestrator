#!/usr/bin/env bash
# Functions defined in <module> that no test file mentions. ponytail: name-based heuristic; use real coverage (pytest-cov / c8) when this misleads.
set -u; mod=${1:?module path}
defs=$(grep -rhoE '(def |function |export (async )?function |fn |func )[A-Za-z_][A-Za-z0-9_]*' "$mod" 2>/dev/null | awk '{print $NF}' | sort -u)
tests=$(git ls-files | grep -iE '(test|spec)s?[./_]' | grep -v node_modules)
for f in $defs; do grep -qw "$f" $tests 2>/dev/null || echo "uncovered: $f  $(grep -rnwE "(def|function|fn|func) $f" "$mod" | head -1 | cut -d: -f1,2)"; done
