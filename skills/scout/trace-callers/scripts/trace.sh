#!/usr/bin/env bash
set -u; sym=${1:?symbol}
if command -v rg >/dev/null; then rg -n --no-heading -g '!node_modules' -g '!dist' -w "$sym" . | head -300
else grep -rnw --exclude-dir=node_modules --exclude-dir=dist "$sym" . | head -300; fi
