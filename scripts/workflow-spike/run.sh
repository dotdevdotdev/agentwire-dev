export WF_SPIKE_DIR="$HOME/wf-spike-scratch"
export WF_SPIKE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export WF_SPIKE_REPO="$(git -C "$WF_SPIKE_SRC" rev-parse --show-toplevel)"
agentwire new -s wf-spike-probe -p "$WF_SPIKE_DIR"
uv run --project "$WF_SPIKE_REPO" python "$WF_SPIKE_SRC/analyze.py" watch wf-spike-probe --dir "$WF_SPIKE_DIR" &
agentwire send -s wf-spike-probe --wait-ready "/wf-spike"
wait
git -C "$WF_SPIKE_DIR" worktree list > "$WF_SPIKE_DIR/.wf-spike/worktrees.after"
uv run --project "$WF_SPIKE_REPO" python "$WF_SPIKE_SRC/analyze.py" report --dir "$WF_SPIKE_DIR"
