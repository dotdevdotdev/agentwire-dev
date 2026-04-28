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

Current implementation in `_build_agent_command_env()` (line 154-162 of `__main__.py`):

```python
if merged.instructions:
    # Write prompt to temp file and read via $(<file) to handle long prompts
    # This avoids issues with command line length limits and escaping
    import tempfile
    prompt_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    prompt_file.write(merged.instructions)
    prompt_file.close()
    env["AGENT_SYSTEM_PROMPT_FILE"] = prompt_file.name
    env["AGENT_SYSTEM_PROMPT_FLAG"] = f'--append-system-prompt "$(<{prompt_file.name})"'
```

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
- [ ] Does `tmux capture-pane` show `quote>` prompts?
- [ ] Is the command under ~8KB total length?
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
