#!/usr/bin/env bash
# graph.sh update|query|affected|explain|summary|status [args] [--repo PATH]
# Wraps the graphify CLI (uv tool `graphifyy`; code-only AST extraction, no LLM, no API key). graphify-out/ lives in the target repo.
set -u
cmd=${1:-status}; shift || true
repo=""; args=()
while [ $# -gt 0 ]; do case "$1" in --repo) repo=$2; shift 2;; *) args+=("$1"); shift;; esac; done
repo=${repo:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
out="$repo/graphify-out"; graph="$out/graph.json"
command -v graphify >/dev/null || { echo "graphify not installed: uv tool install graphifyy" >&2; exit 3; }
need_graph() { [ -f "$graph" ] || { echo "no graph at $graph; run: graph.sh update --repo $repo" >&2; exit 4; }; }
case "$cmd" in
  update)
    # first build and refresh are the same call; --force lets a shrinking graph (deleted code) through
    ( cd "$repo" && graphify update "$repo" "${args[@]+"${args[@]}"}" ) || exit $?
    ( cd "$repo" && graphify reflect --if-stale >/dev/null 2>&1 ) || true
    grep -qxF 'graphify-out/' "$repo/.gitignore" 2>/dev/null || echo "note: add graphify-out/ to $repo/.gitignore" >&2
    n=$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d.get("nodes",[])),"nodes",len(d.get("links",d.get("edges",[]))),"edges")' "$graph" 2>/dev/null || true)
    echo "graph updated: $graph ($n)";;
  query)    need_graph; [ ${#args[@]} -gt 0 ] || { echo 'usage: graph.sh query "<question>" [--dfs] [--budget N]' >&2; exit 2; }
            graphify query "${args[@]}" --graph "$graph";;
  affected) need_graph; graphify affected "${args[@]+"${args[@]}"}" --graph "$graph";;
  explain)  need_graph; graphify explain "${args[@]+"${args[@]}"}" --graph "$graph";;
  summary)  need_graph
            echo "## god nodes"; graphify god-nodes --top "${args[0]:-12}" --graph "$graph" 2>/dev/null
            r="$out/GRAPH_REPORT.md"; [ -f "$r" ] && { echo "## communities"; grep -E '^### Community ' "$r" | sed 's/^### //' | head -30; }
            l="$out/reflections/LESSONS.md"; [ -f "$l" ] && { echo "## lessons"; grep -E '^[-*]' "$l" | head -15; }
            exit 0;;
  status)   if [ -f "$graph" ]; then echo "graph: $graph ($(date -r "$graph" +%F' '%H:%M))"; else echo "graph: none for $repo"; fi;;
  *) echo "usage: graph.sh update|query|affected|explain|summary|status [args] [--repo PATH]" >&2; exit 2;;
esac
