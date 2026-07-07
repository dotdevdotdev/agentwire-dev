#!/usr/bin/env bash
# Benchmark a task across haiku, sonnet, opus — runs all 3 in parallel.
# Requires pre-created worktrees: agentwire-dev-bench-{haiku,sonnet,opus}
#
# Usage: ./scripts/benchmark-models.sh [task-name]
set -euo pipefail

TASK="${1:-cli-test}"
BASE_DIR="$HOME/projects"
MODELS=(haiku sonnet opus)
BENCHMARK_DIR="$HOME/.agentwire/benchmarks"
TIMESTAMP=$(date +%Y-%m-%dT%H-%M-%S)
OUTFILE="$BENCHMARK_DIR/${TASK}-${TIMESTAMP}.md"
TMPDIR=$(mktemp -d)

mkdir -p "$BENCHMARK_DIR"

echo "=== Model Benchmark: $TASK ==="
echo "Parallel mode: 3 worktrees"
echo "Models: ${MODELS[*]}"
echo ""

# Run a single model benchmark, write results to temp files
run_model() {
  local model="$1"
  local project="$BASE_DIR/agentwire-dev-bench-$model"
  local session="bench-$model"

  echo "[$model] Starting..."

  # Ensure clean worktree
  git -C "$project" checkout . 2>/dev/null || true
  mkdir -p "$project/.agentwire"

  # Create session with model override
  agentwire new -s "$session" -p "$project" \
    --posture bypass --roles task-runner \
    --model "$model" -f 2>/dev/null || true

  # Run the task
  local start exit_code=0
  start=$(date +%s)
  agentwire ensure -s "$session" --task "$TASK" --project "$project" --json || exit_code=$?
  local end elapsed
  end=$(date +%s)
  elapsed=$(( end - start ))

  echo "[$model] Done in ${elapsed}s (exit $exit_code)"

  # Find summary file
  local summary_file summary status
  summary_file=$(find "$project/.agentwire" -name "task-summary-${session}-${TASK}-*.md" -print 2>/dev/null \
    | sort -r | head -1)

  if [[ -n "$summary_file" ]]; then
    summary=$(cat "$summary_file")
    status=$(grep -m1 '^status:' "$summary_file" 2>/dev/null | sed 's/status: *//' || echo "unknown")
  else
    summary="(no summary file found)"
    status="no-summary"
  fi

  # Kill session
  agentwire kill -s "$session" 2>/dev/null || true

  # Reset worktree
  git -C "$project" checkout . 2>/dev/null || true

  # Write results to temp files (one per metric for easy reading)
  echo "$elapsed" > "$TMPDIR/${model}.time"
  echo "$status" > "$TMPDIR/${model}.status"
  echo "$exit_code" > "$TMPDIR/${model}.exit"
  echo "$summary" > "$TMPDIR/${model}.summary"
}

# Launch all 3 in parallel
for model in "${MODELS[@]}"; do
  run_model "$model" &
done

echo "Waiting for all models to finish..."
wait
echo ""

# Collect results
declare -a TIMES STATUSES EXITS
for i in "${!MODELS[@]}"; do
  model="${MODELS[$i]}"
  TIMES[$i]="$(cat "$TMPDIR/${model}.time")s"
  STATUSES[$i]="$(cat "$TMPDIR/${model}.status")"
  EXITS[$i]="$(cat "$TMPDIR/${model}.exit")"
done

# Build the report
report="# Model Benchmark: $TASK ($TIMESTAMP)

| Metric    | haiku  | sonnet | opus   |
|-----------|--------|--------|--------|
| Time      | ${TIMES[0]} | ${TIMES[1]} | ${TIMES[2]} |
| Status    | ${STATUSES[0]} | ${STATUSES[1]} | ${STATUSES[2]} |
| Exit code | ${EXITS[0]} | ${EXITS[1]} | ${EXITS[2]} |
"

for model in "${MODELS[@]}"; do
  report+="
## $model

$(cat "$TMPDIR/${model}.summary")
"
done

# Output and save
echo "$report"
echo "$report" > "$OUTFILE"
echo ""
echo "Saved to: $OUTFILE"

# Cleanup temp files
rm -r "$TMPDIR"
