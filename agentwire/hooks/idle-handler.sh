#!/bin/bash
# Handle Claude Code notifications
# Uses agentwire notify-parent for text-only notifications to parent (no audio)
# Worker panes queue notifications with summary file path, then auto-kill

DEBUG_LOG="/tmp/claude-hook-debug.log"
log() { echo "[$(date -Iseconds)] $*" >> "$DEBUG_LOG"; }

# Find agentwire binary (env var > which > default)
AGENTWIRE="${AGENTWIRE_BIN:-$(which agentwire 2>/dev/null || echo "$HOME/.local/bin/agentwire")}"

input=$(cat)
notification_type=$(echo "$input" | jq -r '.notification_type // ""')
log "Hook fired: type=$notification_type TMUX_PANE=$TMUX_PANE"

if [[ "$notification_type" == "idle_prompt" ]]; then
  cwd=$(echo "$input" | jq -r '.cwd // ""')
  session_id=$(echo "$input" | jq -r '.session_id // ""')

  # Get pane info
  pane_index=""
  tmux_session=""
  if [[ -n "$TMUX_PANE" ]]; then
    pane_index=$(tmux display -t "$TMUX_PANE" -p '#{pane_index}' 2>/dev/null)
    tmux_session=$(tmux display -t "$TMUX_PANE" -p '#{session_name}' 2>/dev/null)
  fi

  # Usage-limit park guard: a parked session is waiting out a limit reset —
  # no summary prompts, no /exit, no kill. The watchdog resumes it (#274).
  if [[ -n "$tmux_session" && -f "$HOME/.agentwire/usage-limit/${tmux_session}.json" ]]; then
    log "Session $tmux_session parked on usage limit — skipping idle handling"
    exit 0
  fi

  # Prompt-routing guard (#276): this pane is blocked on an interactive
  # prompt that was routed to its parent. Pasting the summary instruction
  # would answer the dialog (paste + Enter), and killing the pane would reap
  # a worker the parent is about to unblock. Skip until the marker clears.
  if [[ -n "$tmux_session" && -n "$pane_index" && -f "$HOME/.agentwire/prompt-router/${tmux_session}.${pane_index}.json" ]]; then
    log "Pane ${tmux_session}.${pane_index} has a routed prompt pending — skipping idle handling"
    exit 0
  fi

  # Try to get config from .agentwire.yml
  session_name=""
  is_chatbot=false
  parent_session=""

  if [[ -f "$cwd/.agentwire.yml" ]]; then
    session_name=$(grep -E '^session:' "$cwd/.agentwire.yml" 2>/dev/null | sed 's/session:[[:space:]]*//' | tr -d '"' | tr -d "'")
    parent_session=$(grep -E '^parent:' "$cwd/.agentwire.yml" 2>/dev/null | sed 's/parent:[[:space:]]*//' | tr -d '"' | tr -d "'")

    if grep -qE '^-[[:space:]]*chatbot' "$cwd/.agentwire.yml" 2>/dev/null; then
      is_chatbot=true
    fi
  fi

  # Skip for chatbot sessions
  if [[ "$is_chatbot" == true ]]; then
    exit 0
  fi

  # Fallback to directory name
  if [[ -z "$session_name" ]]; then
    session_name=$(basename "$cwd")
  fi

  log "pane_index=$pane_index tmux_session=$tmux_session session_name=$session_name session_id=$session_id"

  # Check if this is a worker pane (pane_index > 0)
  if [[ "$pane_index" != "0" && -n "$pane_index" && -n "$tmux_session" ]]; then
    log "Worker pane detected, starting background job"
    # Worker pane: implement two-pass idle system
    (
      dlog="/tmp/claude-hook-debug.log"
      echo "[$(date -Iseconds)] BG: started pane=$pane_index session=$tmux_session session_id=$session_id" >> "$dlog"

      # Wait 2s for Claude Code to settle
      sleep 2
      echo "[$(date -Iseconds)] BG: after sleep" >> "$dlog"

      # Check if summary file exists
      summary_path="${cwd}/.agentwire/${session_id}.md"
      summary_exists=0
      if [[ -f "$summary_path" ]]; then
        summary_exists=1
      fi
      echo "[$(date -Iseconds)] BG: summary_exists=$summary_exists path=$summary_path" >> "$dlog"

      if [[ "$summary_exists" == "0" ]]; then
        # First idle: No summary file yet, instruct agent to create one
        echo "[$(date -Iseconds)] BG: sending instruction to create summary" >> "$dlog"

        instruction='Please write an exit summary to '"$summary_path"' with these sections:

# Worker Summary

## Task
[What you were asked to do]

## Status
─── DONE ─── (success) | ─── BLOCKED ─── (needs help) | ─── ERROR ─── (failed)

## What I Did
[Actions taken]

## Files Changed
List files you modified or created with brief descriptions

## What Worked
[Successes]

## What Didn'"'"'t Work
[Issues and why]

## Notes for Orchestrator
[Context for follow-up]'

        $AGENTWIRE send --pane "$pane_index" "$instruction" >/dev/null 2>&1 &
        echo "[$(date -Iseconds)] BG: instruction sent" >> "$dlog"
      else
        # Second idle: Summary file exists, read it and notify parent
        echo "[$(date -Iseconds)] BG: reading summary file" >> "$dlog"

        # Read summary content
        summary_content=""
        if command -v cat >/dev/null 2>&1; then
          summary_content=$(cat "$summary_path" 2>/dev/null || echo "")
        fi

        if [[ -n "$summary_content" ]]; then
          message="[WORKER SUMMARY pane ${pane_index}]

${summary_content}"
          echo "[$(date -Iseconds)] BG: message built, queuing notification" >> "$dlog"

          # Queue the notification (same mkdir lock as the queue processor —
          # an append racing the processor's head-trim would lose a message)
          queue_dir="$HOME/.agentwire/queues"
          queue_file="${queue_dir}/${tmux_session}.jsonl"
          lock_dir="${queue_dir}/${tmux_session}.lock"
          mkdir -p "$queue_dir"

          # Append to queue as JSON line
          escaped_message=$(printf '%s' "$message" | jq -Rs .)
          timestamp=$(date +%s)000
          lock_tries=0
          while ! mkdir "$lock_dir" 2>/dev/null; do
            lock_tries=$((lock_tries + 1))
            [[ $lock_tries -ge 50 ]] && break
            sleep 0.1
          done
          echo "{\"timestamp\":${timestamp},\"message\":${escaped_message}}" >> "$queue_file"
          rmdir "$lock_dir" 2>/dev/null
          echo "[$(date -Iseconds)] BG: queued notification" >> "$dlog"

          # Start queue processor if not running
          pid_file="${queue_dir}/${tmux_session}.pid"
          if [[ ! -f "$pid_file" ]] || ! kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null; then
            nohup "$HOME/.agentwire/queue-processor.sh" "$tmux_session" >/dev/null 2>&1 &
            echo "[$(date -Iseconds)] BG: started queue processor" >> "$dlog"
          fi

          # Wait 1s then kill the pane (kill command has its own 3s internal wait)
          sleep 1
          echo "[$(date -Iseconds)] BG: killing pane" >> "$dlog"
          $AGENTWIRE kill --pane "$pane_index" >/dev/null 2>&1 &
        else
          echo "[$(date -Iseconds)] BG: failed to read summary, killing pane anyway" >> "$dlog"
          # Failed to read summary, just kill the pane
          $AGENTWIRE kill --pane "$pane_index" >/dev/null 2>&1 &
        fi
      fi
    ) &
  elif [[ "$pane_index" == "0" && -n "$tmux_session" ]]; then
    # Pane 0: Check for scheduled task context
    task_context_file="$HOME/.agentwire/tasks/${tmux_session}.json"

    if [[ -f "$task_context_file" ]]; then
      log "Scheduled task detected, starting background job"
      # Scheduled task: handle completion (standard or loop mode)
      (
        dlog="/tmp/claude-hook-debug.log"
        echo "[$(date -Iseconds)] TASK: started session=$tmux_session" >> "$dlog"

        # Wait 2s for Claude to settle
        sleep 2

        # Read task context
        task_name=$(jq -r '.task // ""' "$task_context_file" 2>/dev/null)
        summary_file=$(jq -r '.summary_file // ""' "$task_context_file" 2>/dev/null)
        idle_count=$(jq -r '.idle_count // 0' "$task_context_file" 2>/dev/null)
        # NOTE: jq's // operator coerces false to the default (false // true == true),
        # so boolean fields must use an explicit null check to honor a stored false.
        exit_on_complete=$(jq -r 'if .exit_on_complete == null then true else .exit_on_complete end' "$task_context_file" 2>/dev/null)
        mode=$(jq -r '.mode // "standard"' "$task_context_file" 2>/dev/null)
        max_iterations=$(jq -r '.max_iterations // 3' "$task_context_file" 2>/dev/null)
        iteration=$(jq -r '.iteration // 1' "$task_context_file" 2>/dev/null)
        loop_review=$(jq -r 'if .loop_review == null then true else .loop_review end' "$task_context_file" 2>/dev/null)
        loop_delay=$(jq -r '.loop_delay // 0' "$task_context_file" 2>/dev/null)
        original_prompt=$(jq -r '.original_prompt // ""' "$task_context_file" 2>/dev/null)

        # Increment idle count
        new_idle_count=$((idle_count + 1))
        jq ".idle_count = $new_idle_count" "$task_context_file" > "${task_context_file}.tmp" && mv "${task_context_file}.tmp" "$task_context_file"

        echo "[$(date -Iseconds)] TASK: task=$task_name mode=$mode iteration=$iteration/$max_iterations idle_count=$new_idle_count exit_on_complete=$exit_on_complete" >> "$dlog"

        if [[ "$mode" == "loop" ]]; then
          # ─── Loop mode ─────────────────────────────────────────────
          iterations_dir="${cwd}/.agentwire/iterations"
          mkdir -p "$iterations_dir"
          iter_file="${iterations_dir}/${tmux_session}-iter-${iteration}.md"

          if [[ "$loop_review" == "true" ]]; then
            # Two-pass: idle 1 → review prompt, idle 2 → check + decide
            if [[ "$new_idle_count" == "1" ]]; then
              echo "[$(date -Iseconds)] TASK[loop]: sending review prompt for iteration $iteration" >> "$dlog"

              instruction="Review your progress so far. Write a brief status report to ${iter_file}:

# Iteration ${iteration} Review

## Status
complete | incomplete

## What Was Done
[Brief description of work in this iteration]

## Remaining Work
[What still needs to be done, or \"none\" if complete]

Use \"complete\" if the task is fully done. Use \"incomplete\" if more work is needed.
Write the file now."

              $AGENTWIRE send -s "$tmux_session" "$instruction" >/dev/null 2>&1 &
            else
              # Read iteration file for status
              iter_status="incomplete"
              if [[ -f "$iter_file" ]]; then
                iter_status=$(grep -iA1 '## Status' "$iter_file" | tail -1 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
              fi

              echo "[$(date -Iseconds)] TASK[loop]: iteration $iteration status=$iter_status" >> "$dlog"

              if [[ "$iter_status" == "complete" || "$iteration" -ge "$max_iterations" ]]; then
                echo "[$(date -Iseconds)] TASK[loop]: exiting loop (status=$iter_status, iteration=$iteration/$max_iterations)" >> "$dlog"
                # Transition to standard exit
                jq '.mode = "standard" | .idle_count = 0' "$task_context_file" > "${task_context_file}.tmp" && mv "${task_context_file}.tmp" "$task_context_file"
                echo "[$(date -Iseconds)] TASK[loop→standard]: transitioned to standard exit" >> "$dlog"
              else
                # Continue loop
                next_iteration=$((iteration + 1))
                jq ".idle_count = 0 | .iteration = $next_iteration" "$task_context_file" > "${task_context_file}.tmp" && mv "${task_context_file}.tmp" "$task_context_file"

                if [[ "$loop_delay" -gt 0 ]]; then
                  echo "[$(date -Iseconds)] TASK[loop]: waiting ${loop_delay}s before iteration $next_iteration/$max_iterations" >> "$dlog"
                  sleep "$loop_delay"
                fi

                echo "[$(date -Iseconds)] TASK[loop]: continuing to iteration $next_iteration/$max_iterations" >> "$dlog"

                instruction="Continue working on the task. This is iteration ${next_iteration} of ${max_iterations}.

Previous iteration reviews are in ${iterations_dir}/ — read them for context on what's been done.

Original task:
${original_prompt}

Continue where you left off. Focus on remaining work identified in previous reviews."

                $AGENTWIRE send -s "$tmux_session" "$instruction" >/dev/null 2>&1 &
              fi
            fi
          else
            # Single-pass: idle → check cap → re-prompt or exit
            if [[ "$iteration" -ge "$max_iterations" ]]; then
              echo "[$(date -Iseconds)] TASK[loop]: max iterations reached ($iteration/$max_iterations), exiting" >> "$dlog"
              jq '.mode = "standard" | .idle_count = 0' "$task_context_file" > "${task_context_file}.tmp" && mv "${task_context_file}.tmp" "$task_context_file"
              echo "[$(date -Iseconds)] TASK[loop→standard]: transitioned to standard exit" >> "$dlog"
            else
              next_iteration=$((iteration + 1))
              jq ".idle_count = 0 | .iteration = $next_iteration" "$task_context_file" > "${task_context_file}.tmp" && mv "${task_context_file}.tmp" "$task_context_file"

              if [[ "$loop_delay" -gt 0 ]]; then
                echo "[$(date -Iseconds)] TASK[loop]: waiting ${loop_delay}s before iteration $next_iteration/$max_iterations" >> "$dlog"
                sleep "$loop_delay"
              fi

              echo "[$(date -Iseconds)] TASK[loop]: continuing to iteration $next_iteration/$max_iterations" >> "$dlog"

              instruction="Continue working on the task. This is iteration ${next_iteration} of ${max_iterations}.

Previous iteration reviews are in ${iterations_dir}/ — read them for context on what's been done.

Original task:
${original_prompt}

Continue where you left off. Focus on remaining work identified in previous reviews."

              $AGENTWIRE send -s "$tmux_session" "$instruction" >/dev/null 2>&1 &
            fi
          fi
        else
          # ─── Standard mode ─────────────────────────────────────────
          if [[ "$new_idle_count" == "1" ]]; then
            # First idle: send summary prompt
            echo "[$(date -Iseconds)] TASK[standard]: first idle, sending summary prompt" >> "$dlog"
            summary_path="${cwd}/${summary_file}"

            instruction="Task complete. Write a brief summary to ${summary_path} with:
# Task Summary
## Status
complete | incomplete | error
## What Was Done
[Brief description]
## Notes
[Any important context]"

            $AGENTWIRE send -s "$tmux_session" "$instruction" >/dev/null 2>&1 &
            echo "[$(date -Iseconds)] TASK[standard]: summary prompt sent" >> "$dlog"
          else
            # Second+ idle: optionally exit session (ensure polls summary file directly)
            echo "[$(date -Iseconds)] TASK[standard]: second idle" >> "$dlog"

            if [[ "$exit_on_complete" == "true" ]]; then
              echo "[$(date -Iseconds)] TASK: exit_on_complete=true, sending /exit" >> "$dlog"
              sleep 1
              $AGENTWIRE send -s "$tmux_session" "/exit" >/dev/null 2>&1

              # Clean up task context file so it doesn't haunt future sessions
              rm "$task_context_file" 2>/dev/null
              echo "[$(date -Iseconds)] TASK: cleaned up task context" >> "$dlog"

              # Wait for Claude to exit, then kill the tmux session
              sleep 3
              echo "[$(date -Iseconds)] TASK: killing tmux session" >> "$dlog"
              tmux kill-session -t "$tmux_session" 2>/dev/null &
            else
              # Persistent session (exit_on_complete: false): leave it running,
              # but still remove the context file — ensure's completion poll
              # blocks until it's gone, and later interactive idles must take
              # the normal (non-task) path.
              rm "$task_context_file" 2>/dev/null
              echo "[$(date -Iseconds)] TASK: exit_on_complete=false, session left alive, cleaned up task context" >> "$dlog"
            fi
          fi
        fi
      ) &
    else
      # No task context file - check if this might be a scheduled task that lost its context
      # Look for recent task summary files scoped to THIS session (avoids false matches
      # when multiple sessions share the same project directory)
      recent_summary=$(find "${cwd}/.agentwire" -name "task-summary-${tmux_session}-*.md" -mmin -5 2>/dev/null | head -1)

      if [[ -n "$recent_summary" ]]; then
        log "No task context but found recent summary file, cleaning up session"
        # Task appears to have completed but context was cleared - try to exit gracefully
        (
          dlog="/tmp/claude-hook-debug.log"
          echo "[$(date -Iseconds)] TASK-ORPHAN: found summary at $recent_summary, exiting session" >> "$dlog"
          sleep 1
          $AGENTWIRE send -s "$tmux_session" "/exit" >/dev/null 2>&1

          # Clean up orphan summary so it doesn't trigger again
          rm "$recent_summary" 2>/dev/null
          echo "[$(date -Iseconds)] TASK-ORPHAN: cleaned up orphan summary" >> "$dlog"

          sleep 3
          echo "[$(date -Iseconds)] TASK-ORPHAN: killing tmux session" >> "$dlog"
          tmux kill-session -t "$tmux_session" 2>/dev/null &
        ) &
      else
        # Pane-0 session went idle: tell its parent. Resolution lives inside
        # notify-parent (prompt_router.resolve_parent) so all three precedence
        # sources are honored — worker pane → pane 0, creator recorded at
        # `agentwire new` time (session metadata), then `.agentwire.yml parent:`.
        # The old code passed only the .agentwire.yml value and skipped the call
        # entirely when it was empty, so worktree / `agentwire new` children
        # (whose parent lives in metadata, not config) never notified anyone.
        # TMUX_PANE is inherited, so the CLI knows which session this is; a
        # top-level session resolves to no parent and this is a harmless no-op.
        # --queued (#667): ride the polite msg inbox (kind=done) instead of a
        # direct paste — a busy orchestrator defers the message instead of
        # accumulating unsubmitted notify lines in its input box, and an
        # undeliverable one dead-letters + emails the owner instead of
        # vanishing. Failures are logged, never discarded.
        (
          out=$($AGENTWIRE notify-parent --on-idle --queued -q "is idle and done working" 2>&1) \
            || log "notify-parent --queued failed: $out"
        ) &
      fi
    fi
  fi
fi

exit 0
