#!/usr/bin/env bash
# tree (depth 3, no vendor dirs) + one line per top-level symbol. ponytail: grep-based; swap in ctags/tree-sitter when this gets noisy.
set -u
git ls-files | grep -vE 'node_modules|\.lock$|dist/|build/' | awk -F/ 'NF<=3' | sort | head -400
echo "## symbols"
git ls-files '*.py' '*.ts' '*.tsx' '*.js' '*.go' '*.rs' '*.swift' '*.gd' 2>/dev/null | grep -vE 'node_modules|\.d\.ts$|test' \
 | xargs grep -nE '^(export )?(async )?(def |class |function |func |fn |struct |interface |type |const [A-Z])' 2>/dev/null | head -600
