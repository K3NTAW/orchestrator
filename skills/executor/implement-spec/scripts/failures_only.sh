#!/usr/bin/env bash
# Pipe test output through this: keeps failing test names + assertion lines, drops passes and noise.
grep -E 'FAIL|ERROR|Error|error TS|✗|✕|AssertionError|assert |Expected|Received|Traceback|^\s+File ' | grep -vE 'passed|PASS' | head -60
