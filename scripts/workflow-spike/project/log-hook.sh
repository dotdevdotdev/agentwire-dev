#!/usr/bin/env bash
set -u
out="${CLAUDE_PROJECT_DIR:-$PWD}/.wf-spike"
mkdir -p "$out"
jq -c --argjson pid "$$" '. + {_ts: (now | floor), _pid: $pid}' >> "$out/events.jsonl"
exit 0
