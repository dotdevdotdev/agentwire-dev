#!/bin/bash
# Queue processor for agentwire notifications
# Sends queued alerts with 15-second gaps to prevent overwhelming orchestrators

DEBUG_LOG="/tmp/queue-processor-debug.log"
log() { echo "[$(date -Iseconds)] $*" >> "$DEBUG_LOG"; }

# Clear tmux context so alert command doesn't think we're in a pane
unset TMUX TMUX_PANE

# Load env vars for Telegram notifications
for envfile in "$HOME/.agentwire/.env" ".env"; do
    [[ -f "$envfile" ]] && while IFS='=' read -r key value; do
        [[ -n "$key" && "$key" != \#* ]] && export "$key=$value"
    done < "$envfile"
done

# Find agentwire binary (env var > which > default)
AGENTWIRE="${AGENTWIRE_BIN:-$(which agentwire 2>/dev/null || echo "$HOME/.local/bin/agentwire")}"

SESSION="$1"
log "Started for session=$SESSION agentwire=$AGENTWIRE"
QUEUE_FILE="$HOME/.agentwire/queues/${SESSION}.jsonl"
PID_FILE="$HOME/.agentwire/queues/${SESSION}.pid"
DELAY=15

# Write our PID
echo $$ > "$PID_FILE"

# Cleanup on exit
cleanup() {
    rm -f "$PID_FILE"
}
trap cleanup EXIT

# Process queue until empty
while true; do
    # Check if queue file exists and has content
    if [[ ! -f "$QUEUE_FILE" ]] || [[ ! -s "$QUEUE_FILE" ]]; then
        # Queue empty, exit
        exit 0
    fi

    # Read first line
    LINE=$(head -n 1 "$QUEUE_FILE")

    if [[ -z "$LINE" ]]; then
        exit 0
    fi

    # Extract message from JSON using jq
    MESSAGE=$(echo "$LINE" | jq -r '.message // ""' 2>/dev/null)

    if [[ -n "$MESSAGE" ]]; then
        log "Sending alert to $SESSION: ${MESSAGE:0:50}..."
        # Send alert to session's pane 0
        if "$AGENTWIRE" alert -q --to "$SESSION" "$MESSAGE" 2>>"$DEBUG_LOG"; then
            log "Alert sent successfully"
        else
            log "Alert failed with exit code $?"
        fi
    else
        log "Empty message, skipping"
    fi

    # Remove first line from queue (atomic via temp file)
    TEMP_FILE=$(mktemp)
    tail -n +2 "$QUEUE_FILE" > "$TEMP_FILE" 2>/dev/null
    mv "$TEMP_FILE" "$QUEUE_FILE"

    # Check if more items remain
    if [[ ! -s "$QUEUE_FILE" ]]; then
        exit 0
    fi

    # Wait before processing next
    sleep $DELAY
done
