#!/bin/bash
# AgentWire Recommended Claude Code Status Line
#
# Line 1: model   folder   branch
# Line 2: full-width context battery
#
# Install: cp to ~/.claude/statusline.sh, chmod +x, and point
# ~/.claude/settings.json at it:
#   "statusLine": { "type": "command", "command": "~/.claude/statusline.sh" }

set -e

JSON=$(cat)

# One jq pass for every field — the statusline re-renders constantly, so
# separate jq spawns per render is the whole cost of this script.
eval "$(printf '%s' "$JSON" | jq -r '
  @sh "CURRENT_DIR=\(.workspace.current_dir // "")",
  @sh "MODEL_ID=\((.model|objects|.id) // (.model|objects|.display_name) // (.model|strings) // "")",
  @sh "USED_PCT=\(if .context_window.used_percentage then (.context_window.used_percentage|floor|tostring) else "" end)"
' 2>/dev/null)"

# ─── Directory ────────────────────────────────────────────────
if [ -n "$CURRENT_DIR" ]; then
  DIR_SHORT=$(echo "$CURRENT_DIR" | sed "s|$HOME|~|")
  if [ ${#DIR_SHORT} -gt 30 ]; then
    DIR_SHORT="…/$(echo "$DIR_SHORT" | rev | cut -d'/' -f1-2 | rev)"
  fi
else
  DIR_SHORT="?"
fi

# ─── Git branch ──────────────────────────────────────────────
GIT_BRANCH=""
if [ -n "$CURRENT_DIR" ]; then
  GIT_BRANCH=$(git -C "$CURRENT_DIR" branch --show-current 2>/dev/null || echo "")
fi

# ─── Model ───────────────────────────────────────────────────
MODEL_SHORT=""
if [ -n "$MODEL_ID" ]; then
  case "$MODEL_ID" in
    *opus*) MODEL_SHORT="opus" ;;
    *sonnet*) MODEL_SHORT="sonnet" ;;
    *haiku*) MODEL_SHORT="haiku" ;;
    *fable*) MODEL_SHORT="fable" ;;
    *) MODEL_SHORT="$MODEL_ID" ;;
  esac
fi

# ─── Context battery (pre-calculated percentage) ─────────────
REMAINING=""
BAT_COLOR=""
if [ -n "$USED_PCT" ] && [ "$USED_PCT" != "0" ]; then
  REMAINING=$((100 - USED_PCT))
  [ "$REMAINING" -lt 0 ] && REMAINING=0

  if [ "$REMAINING" -gt 50 ]; then
    BAT_COLOR='\033[0;32m'
  elif [ "$REMAINING" -gt 25 ]; then
    BAT_COLOR='\033[0;33m'
  else
    BAT_COLOR='\033[0;31m'
  fi
fi

# ─── Colors ──────────────────────────────────────────────────
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
MAGENTA='\033[0;35m'
RESET='\033[0m'

# ─── Terminal width ──────────────────────────────────────────
# Detect live width per render. Sources, most reliable first:
#   1. tmux pane width  — correct inside tmux/agentwire panes, where
#      $COLUMNS is unset and `tput cols` can't measure a piped stdout.
#   2. $COLUMNS          — exported by Claude Code in a plain terminal.
#   3. tput cols         — last resort (often the static 80 default here).
WIDTH=""
if [ -n "$TMUX_PANE" ] && command -v tmux >/dev/null 2>&1; then
  WIDTH=$(tmux display -p -t "$TMUX_PANE" '#{pane_width}' 2>/dev/null)
fi
[ -z "$WIDTH" ] && WIDTH=$COLUMNS
[ -z "$WIDTH" ] && WIDTH=$(tput cols 2>/dev/null)
case "$WIDTH" in ''|*[!0-9]*) WIDTH=80 ;; esac
[ "$WIDTH" -lt 20 ] && WIDTH=80
# Inset margin: the statusline is padded a column or two each side,
# so leave room or the bar wraps to a phantom extra line.
WIDTH=$((WIDTH - 4))

# ─── Line 1: model  folder  branch ───────────────────────────
SEP="  "
LINE1=""
add_field() { # $1 = colored
  if [ -n "$LINE1" ]; then LINE1="${LINE1}${SEP}$1"; else LINE1="$1"; fi
}
[ -n "$MODEL_SHORT" ] && add_field "${MAGENTA}${MODEL_SHORT}${RESET}"
add_field "${CYAN}${DIR_SHORT}${RESET}"
[ -n "$GIT_BRANCH" ] && add_field "${YELLOW}${GIT_BRANCH}${RESET}"

# ─── Line 2: full-width context battery ──────────────────────
# [███████████████████████████████░░░░░░░░] 80%
LINE2=""
if [ -n "$REMAINING" ]; then
  LABEL=" ${REMAINING}%"
  # Reserve room for the brackets and the percentage label.
  BAR_WIDTH=$((WIDTH - ${#LABEL} - 2))
  [ "$BAR_WIDTH" -lt 1 ] && BAR_WIDTH=1
  FILLED=$((REMAINING * BAR_WIDTH / 100))
  [ "$FILLED" -gt "$BAR_WIDTH" ] && FILLED=$BAR_WIDTH
  [ "$FILLED" -lt 0 ] && FILLED=0
  EMPTY=$((BAR_WIDTH - FILLED))
  BAR=$(printf '█%.0s' $(seq 1 $FILLED 2>/dev/null) || true)
  BAR="${BAR}$(printf '░%.0s' $(seq 1 $EMPTY 2>/dev/null) || true)"
  LINE2="${BAT_COLOR}[${BAR}]${LABEL}${RESET}"
fi

printf '%b\n' "$LINE1"
if [ -n "$LINE2" ]; then
  printf '%b\n' "$LINE2"
fi
