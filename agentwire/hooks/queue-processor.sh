#!/bin/bash
# Queue processor for agentwire notifications
# Sends queued messages with 15-second gaps to prevent overwhelming orchestrators.
# Delivery is `agentwire notify-parent --raw` (verified paste + pre-paste safety
# checks); failed deliveries are requeued with an attempt count instead of dropped.

DEBUG_LOG="/tmp/queue-processor-debug.log"
log() { echo "[$(date -Iseconds)] $*" >> "$DEBUG_LOG"; }

# Clear tmux context so notify-parent doesn't think we're in a pane
unset TMUX TMUX_PANE

# Load env vars
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
LOCK_DIR="$HOME/.agentwire/queues/${SESSION}.lock"
DELAY=15
MAX_ATTEMPTS=20  # ~5 minutes of 15s retries before a message is dropped

# Write our PID
echo $$ > "$PID_FILE"

# Cleanup on exit
cleanup() {
    rm -f "$PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null
}
trap cleanup EXIT

# mkdir-based lock around queue mutation (macOS has no flock(1)); appenders
# (idle-handler, prompt_router.enqueue) take the same lock. Best-effort: after
# a 5s spin we proceed anyway rather than wedge the queue forever.
acquire_lock() {
    local tries=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        tries=$((tries + 1))
        if [[ $tries -ge 50 ]]; then
            log "Lock timeout, proceeding without lock"
            return 0
        fi
        sleep 0.1
    done
}
release_lock() { rmdir "$LOCK_DIR" 2>/dev/null; }

# Process queue until empty
while true; do
    if [[ ! -f "$QUEUE_FILE" ]] || [[ ! -s "$QUEUE_FILE" ]]; then
        exit 0
    fi

    LINE=$(head -n 1 "$QUEUE_FILE")

    if [[ -z "$LINE" ]]; then
        exit 0
    fi

    MESSAGE=$(echo "$LINE" | jq -r '.message // ""' 2>/dev/null)
    ATTEMPTS=$(echo "$LINE" | jq -r '.attempts // 0' 2>/dev/null)
    DELIVERED=true

    if [[ -n "$MESSAGE" ]]; then
        log "Sending to $SESSION (attempt $((ATTEMPTS + 1))): ${MESSAGE:0:50}..."
        if "$AGENTWIRE" notify-parent -q --raw --to "$SESSION" "$MESSAGE" 2>>"$DEBUG_LOG"; then
            log "Delivered"
        else
            DELIVERED=false
            log "Delivery refused/failed (exit $?)"
        fi
    else
        log "Empty message, dropping"
    fi

    # Mutate the queue under the lock: drop the head; if delivery failed and
    # attempts remain, requeue at the tail with attempts+1.
    acquire_lock
    TEMP_FILE=$(mktemp)
    tail -n +2 "$QUEUE_FILE" > "$TEMP_FILE" 2>/dev/null
    if [[ "$DELIVERED" == false && -n "$MESSAGE" ]]; then
        if [[ "$ATTEMPTS" -lt "$MAX_ATTEMPTS" ]]; then
            echo "$LINE" | jq -c '.attempts = ((.attempts // 0) + 1)' >> "$TEMP_FILE" 2>/dev/null
        else
            log "Dropping message after $ATTEMPTS attempts: ${MESSAGE:0:80}"
        fi
    fi
    mv "$TEMP_FILE" "$QUEUE_FILE"
    release_lock

    if [[ ! -s "$QUEUE_FILE" ]]; then
        exit 0
    fi

    sleep $DELAY
done
