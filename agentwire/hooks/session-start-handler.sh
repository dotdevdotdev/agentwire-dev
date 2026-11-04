#!/bin/bash
# Claude Code SessionStart hook (source: startup|resume|clear|compact).
#
# Claude mints a new conversation id on /clear and switches ids on /resume;
# nothing at launch can know. This reports the id to the owning agentwire
# session's record so `agentwire restart` resumes the CURRENT conversation
# (#1271). The logic lives in `agentwire hooks session-start` (CLI is SSOT);
# this wrapper only hands over the payload.
#
# Never blocks the session start and never fails it: the work runs detached
# and the script exits 0 unconditionally.

DEBUG_LOG="/tmp/claude-hook-debug.log"
AGENTWIRE="${AGENTWIRE_BIN:-$(which agentwire 2>/dev/null || echo "$HOME/.local/bin/agentwire")}"

input=$(cat)
[[ -n "$TMUX_PANE" ]] || exit 0

(
  printf '%s' "$input" | "$AGENTWIRE" hooks session-start 2>&1 \
    | sed "s|^|[$(date -Iseconds)] session-start: |" >> "$DEBUG_LOG"
) </dev/null >/dev/null 2>&1 &
exit 0
