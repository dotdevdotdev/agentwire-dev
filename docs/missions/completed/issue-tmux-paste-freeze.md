# FIXED: tmux freezes when pasting large content via portal

## Problem

Pasting larger content into a portal session frequently causes tmux to freeze/hang. The issue occurs in the portal more than in tmux directly.

## Root Causes (Two Separate Code Paths)

### 1. Terminal mode (xterm.js) — the main culprit

`server.py:1185-1192` — The terminal WebSocket writes paste data directly to the PTY master fd via `os.write()`. When xterm.js sends a large paste, it dumps everything at once, overwhelming tmux's input buffer and causing a freeze.

```python
# Before: dumps entire paste at once
os.write(master_fd, filtered_data.encode())
```

### 2. API/CLI path (send_input)

`agents/tmux.py:339-380` — `send_input()` uses `tmux send-keys -l` for ALL input regardless of size. This sends text character-by-character, which is slow and can block tmux on large payloads.

## Fixes Applied

### Fix 1: Chunked PTY writes for terminal mode

`server.py` — Break large pastes into 512-byte chunks with 10ms async delays. Applies to both local (PTY) and remote (stdin) terminal connections.

```python
# After: chunk large pastes to avoid overwhelming tmux
data = filtered_data.encode()
CHUNK_SIZE = 512
if len(data) > CHUNK_SIZE:
    for i in range(0, len(data), CHUNK_SIZE):
        os.write(master_fd, data[i:i+CHUNK_SIZE])
        await asyncio.sleep(0.01)
else:
    os.write(master_fd, data)
```

### Fix 2: load-buffer + paste-buffer for send_input

`agents/tmux.py` — For anything >10 chars or multi-line, use `tmux load-buffer + paste-buffer` instead of `send-keys`. Writes text to a temp file, loads into tmux buffer, then pastes as a single unit.

```python
use_buffer = len(text) > 10 or "\n" in text

if use_buffer:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(text)
        temp_path = f.name
    try:
        self._run_local(["tmux", "load-buffer", temp_path])
        self._run_local(["tmux", "paste-buffer", "-t", session_name])
    finally:
        os.unlink(temp_path)
```

### Fix 3: Consistent threshold in pane_manager

`pane_manager.py` — Lowered the `load-buffer` threshold from 200 to 10 chars to match.

### Fix 4: load-buffer for CLI send command

`__main__.py` `cmd_send()` — Same `load-buffer + paste-buffer` approach for both local and remote sends when >10 chars.

## Additional: xclip freeze (Linux)

On Linux, clipboard commands like `xclip` can freeze tmux by waiting on STDIN. Fix by redirecting:

```bash
# Freezes:
bind C-c run "tmux save-buffer - | xclip -i -sel clipboard"

# Fixed:
bind C-c run "tmux save-buffer - | xclip -i -sel clipboard &>/dev/null"
```

Relevant for Linux devboxes if clipboard integration is added later.

## Affected Files

- `agentwire/server.py` — terminal WebSocket chunked writes (Fix 1)
- `agentwire/agents/tmux.py` — `send_input()` load-buffer (Fix 2)
- `agentwire/pane_manager.py` — threshold lowered to 10 (Fix 3)
- `agentwire/__main__.py` — `cmd_send()` load-buffer (Fix 4)

## Severity

High — blocked normal workflow when pasting content via the portal.
