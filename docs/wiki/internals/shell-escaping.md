# Shell Escaping in AgentWire

> Living document. Update this, don't create new versions.

This document covers the challenges of passing complex strings (like role instructions) through tmux to shell commands.

---

## The Problem

AgentWire sends commands to tmux sessions via `tmux send-keys`. This means:

1. Python builds a command string
2. `subprocess.run(["tmux", "send-keys", "-t", session, command, "Enter"])` sends it
3. tmux types each character into the shell
4. bash interprets the command

**Key insight:** `tmux send-keys` sends literal keystrokes. If your command contains a newline character, tmux sends the Enter key, which executes an incomplete command.

---

## Failed Approaches

### Approach 1: Embedded Quotes with Escaping

```python
# DON'T DO THIS
escaped = text.replace('"', '\\"')
cmd = f'claude --append-system-prompt "{escaped}"'
```

**Problem:** Newlines in `text` become Enter keypresses, breaking the command mid-string.

```
% claude --append-system-prompt "line 1
quote> line 2"   # Bash waiting for closing quote - broken!
```

### Approach 2: Bash $'...' Quoting

```python
# DON'T DO THIS
escaped = text.replace("'", "\\'").replace('\n', '\\n')
cmd = f"claude --append-system-prompt $'{escaped}'"
```

**Problem:** In bash `$'...'`, `\n` is interpreted as an actual newline. So we'd need `\\n`:

```python
escaped = text.replace('\n', '\\\\n')  # Double escape
```

But then Claude Code receives literal `\n` characters, not newlines. Claude Code does NOT interpret `\n` escape sequences in `--append-system-prompt`.

### Approach 3: printf with Command Substitution

```python
# DON'T DO THIS for long strings
escaped = text.replace("'", "'\"'\"'").replace('\n', '\\n')
cmd = f"claude --append-system-prompt \"$(printf '%b' '{escaped}')\""
```

**Problem:** Works for short strings, but for long role files (6KB+), the command line becomes unwieldy and can have display/wrapping issues in tmux.

---

## The Solution: Temp File

Write the content to a file and read it via bash `$(<file)` substitution:

```python
import tempfile

prompt_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
prompt_file.write(merged.instructions)
prompt_file.close()

cmd = f'claude --append-system-prompt "$(<{prompt_file.name})"'
```

**Why this works:**
1. The command sent to tmux is short: `claude --append-system-prompt "$(<'/tmp/tmpXXX.txt')"`
2. Bash reads the file contents at execution time
3. File contents preserve newlines and special characters exactly
4. No escaping needed

**Result:** The short command can be safely sent via tmux, and bash handles the file reading.

---

## Implementation Details

Current implementation in `build_agent_command()` (`agentwire/core.py:216`, the `--append-system-prompt` line at `core.py:274`):

```python
if merged.instructions:
    # Write to temp file to avoid shell escaping issues
    # MUST be last flag — multiline content can break subsequent args
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    f.write(merged.instructions)
    f.close()
    temp_file = f.name
    parts.append(f'--append-system-prompt "$(<{temp_file})"')
```

The temp file path is returned on `AgentCommand.temp_file` so the caller can clean it up after the agent starts.

---

## The Second Trap: the launch line has a 1024-byte ceiling (#856)

The temp-file trick keeps the launch line *short-ish*, not short. It still has
to be **typed** into the pane, and the pane's tty imposes a hard per-line cap
that has nothing to do with quoting.

`_launch_tmux_session` fires `send-keys` 0.1s after `tmux new-session` —
before the shell has switched its tty out of **canonical (cooked) mode**. In
canonical mode the line discipline buffers input until a newline, and that
buffer is capped: **1024 bytes on macOS** (`MAX_CANON` / `N_TTY_BUF_SIZE`;
Linux is 4096). Everything past the cap is discarded **silently** — no error,
no partial-write signal.

Because the launch line ends in `--append-system-prompt "$(<…)"`, a truncated
one is syntactically incomplete. zsh parks at a continuation prompt, `claude`
never runs, and the session is a **bare shell** — the exact zombie shape
`agentwire/scheduler/zombie.py` reaps, and all `ensure` can say is
`Agent not running in session '<name>'`.

Measured (2026-08-03, `tmux send-keys` 0.1s after `new-session`, macOS):

| Typed line length | Arrives intact? |
|---|---|
| 500 / 1000 / 1024 | yes |
| 1045 / 1200 / 1500 | **no — tail silently dropped** |

After the shell settles (~1s+, ZLE has the tty in raw mode) the cap is gone
and a 1500-char line lands fine. That's why the bug reads as flaky and why it
tracks *session name length*: #742/#743 grew `_guarded_launch_command` by
~700 chars (it interpolates the worktree path **four** times), so a scheduler
worktree with a long name — `scheduler-ai-morning-briefing-<timestamp>` —
crossed 1024 while `scheduler-weekly-stars-<timestamp>` stayed under it. Same
code, one task broken every night, the rest fine.

### The fix: carry the command in the env, don't type it

`tmux new-session -e K=V` is protocol data, not keyboard input, so it has no
tty cap. The launch line rides in as `AGENTWIRE_LAUNCH_CMD` and the pane is
sent a fixed ~70-char line instead:

```python
LAUNCH_CMD_ENV = "AGENTWIRE_LAUNCH_CMD"
_LAUNCH_EVAL = f'eval "${{{LAUNCH_CMD_ENV}:?agentwire: launch command not injected}}"'
```

The typed line's length is now **independent of** the path, the posture flags,
the model override, and the role temp-file path. `:?` makes a missing var
loud instead of another silent bare shell.

**Why not `paste-buffer`?** A buffer paste goes through the same pane input
path and hits the same cap. It's the right tool for prompts sent to a
*running* Claude (raw mode — see `pane_manager.send_to_target`), not for the
pre-agent shell.

**Rule for new code:** never type anything of unbounded length into a
freshly-created pane. Inject it (`-e`) and `eval`, or write it to a file and
`source` it.

---

## Testing Shell Escaping

When debugging escaping issues:

### Check if Claude is running

```bash
pgrep -f "claude.*dangerously"
```

### Capture tmux pane to see what was typed

```bash
tmux capture-pane -t session-name -p -S -50
```

### Look for quote prompts

If you see `quote>` or `dquote>` in the output, bash is waiting for a closing quote - the command was broken.

### Test simple cases first

```bash
# This should work
tmux send-keys -t test 'echo "hello world"' Enter

# This breaks (newline becomes Enter)
tmux send-keys -t test $'echo "hello\nworld"' Enter
```

---

## Guidelines for Future Development

### When sending commands via tmux

1. **Avoid embedding long strings** - Use temp files instead
2. **Avoid actual newlines** - They become Enter keypresses
3. **Test with role files** - They're the most complex case
4. **Check that Claude actually starts** - Don't just look at tmux display

### When adding new CLI flags that accept content

1. Consider if users might pass multi-line content
2. If so, provide a `-file` variant or use temp files internally
3. Document any escaping requirements

### Debug checklist

- [ ] Does `pgrep` show the process running?
- [ ] Does `tmux capture-pane` show `quote>` prompts, or a line that just
      *stops* mid-token? (Truncation, not quoting — see the 1024-byte cap.)
- [ ] Is the line **typed into a fresh pane** under 1024 bytes? Anything
      longer must be injected via `-e` + `eval`, not typed.
- [ ] Are there any unescaped special characters?

---

## Reference: Bash Quoting

| Syntax | Behavior |
|--------|----------|
| `"..."` | Expands variables, interprets `\$`, `\\`, `` \` ``, `\"` |
| `'...'` | Literal string, no interpretation |
| `$'...'` | Interprets escape sequences (`\n`, `\t`, etc.) |
| `$(<file)` | Reads file contents as string |

For complex content with newlines and special characters, `$(<file)` is the safest approach.
