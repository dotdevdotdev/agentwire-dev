set -euo pipefail
export WF_SPIKE_DIR="$HOME/wf-spike-scratch"
export WF_SPIKE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
mkdir "$WF_SPIKE_DIR"
mkdir -p "$WF_SPIKE_DIR/.claude/workflows" "$WF_SPIKE_DIR/.wf-spike"
cp "$WF_SPIKE_SRC/project/settings.json" "$WF_SPIKE_DIR/.claude/settings.json"
cp "$WF_SPIKE_SRC/project/log-hook.sh" "$WF_SPIKE_DIR/.claude/log-hook.sh"
cp "$WF_SPIKE_SRC/project/wf-spike.js" "$WF_SPIKE_DIR/.claude/workflows/wf-spike.js"
printf '# wf-spike scratch\n' > "$WF_SPIKE_DIR/README.md"
printf 'start\n' > "$WF_SPIKE_DIR/notes.txt"
printf '.wf-spike/\n.claude/\n' > "$WF_SPIKE_DIR/.gitignore"
git -C "$WF_SPIKE_DIR" init -q -b main
git -C "$WF_SPIKE_DIR" add -A
git -C "$WF_SPIKE_DIR" -c user.name=spike -c user.email=spike@example.invalid commit -q -m init
git -C "$WF_SPIKE_DIR" worktree list > "$WF_SPIKE_DIR/.wf-spike/worktrees.before"
echo "scratch project ready: $WF_SPIKE_DIR"
