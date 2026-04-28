# Issue: Remote TTS Session Detection Fails

## Summary

When an agent on a remote machine calls `agentwire_say()` via MCP, TTS fails because the CLI cannot detect the tmux session. Falls back to local TTS which fails with "TTS backend 'none' not supported for local playback".

## Root Cause

The MCP server subprocess doesn't inherit the `TMUX` environment variable from the parent tmux session. Without this, `_get_current_tmux_session()` returns `None`, and the CLI can't determine which session to check for portal connections.

**Flow:**
1. Agent calls `agentwire_say(text="...")` (no session param)
2. MCP server runs `agentwire say "text"`
3. CLI tries: `args.session or _get_current_tmux_session() or _infer_session_from_path()`
4. `_get_current_tmux_session()` fails (no TMUX env var)
5. `_infer_session_from_path()` may return wrong name
6. Portal connection check fails or uses wrong session name
7. Falls back to local TTS → fails because `tts.backend: none`

## Current Workaround

Add `TMUX` env var to the MCP config on the remote machine:

```json
{
  "mcpServers": {
    "agentwire": {
      "command": "agentwire",
      "args": ["mcp"],
      "env": {
        "TMUX": "/tmp/tmux-1000/default,<PID>,0"
      }
    }
  }
}
```

**Problem:** PID changes on tmux restart, so this breaks.

## Proposed Solutions

### Option 1: Add `AGENTWIRE_SESSION` env var (Recommended)

Add support for `AGENTWIRE_SESSION` env var in `cmd_say()`:

```python
session = args.session or os.environ.get("AGENTWIRE_SESSION") or _get_current_tmux_session() or _infer_session_from_path()
```

MCP config would then use:
```json
"env": { "AGENTWIRE_SESSION": "jordan@jordan-devbox" }
```

This is stable across restarts.

### Option 2: Use `machine_id` from config

Already partially implemented. In `_check_portal_connections()`, I added:

```python
config = load_config()
machine_id = config.get("machine_id")
if machine_id:
    session_variants.append(f"{session}@{machine_id}")
```

But this requires the remote to have the updated code (not just PyPI version).

### Option 3: MCP server auto-detects session

Have the MCP server detect the session once at startup and pass it to all CLI calls:

```python
# In mcp_server.py
def get_current_session() -> str | None:
    """Detect session from TMUX env or config."""
    if tmux := os.environ.get("TMUX"):
        # Parse and query tmux
        ...
    config = load_config()
    return config.get("default_session") or config.get("machine_id")
```

Then in `say()`:
```python
if not session:
    session = get_current_session()
```

### Option 4: Add `default_session` config field

Allow config to specify a default target session for audio routing:

```yaml
# ~/.agentwire/config.yaml on remote machine
default_session: "agentwire"  # Route audio to main browser
```

In `cmd_say()`:
```python
config = load_config()
session = (args.session
           or os.environ.get("AGENTWIRE_SESSION")
           or _get_current_tmux_session()
           or config.get("default_session")
           or _infer_session_from_path())
```

This is useful when the browser is always viewing a specific session (like `agentwire`), and remote agents should route audio there regardless of their own session name.

## Files Involved

- `agentwire/__main__.py` - `cmd_say()`, `_get_current_tmux_session()`, `_check_portal_connections()`
- `agentwire/mcp_server.py` - `say()` tool, `run_agentwire_cmd()`

## Related Setup Done

**jordan-devbox (134.122.35.134):**
1. Created `~/.agentwire/config.yaml` with `portal.url`, `machine_id`, `default_session`
2. Set up reverse SSH tunnel: `ssh -R 8765:localhost:8765 dev@134.122.35.134`
3. Renamed tmux session to `jordan@jordan-devbox`
4. Agentwire v1.1.0 installed via uv

**eric-devbox (138.197.145.5):**
1. Created `~/.agentwire/config.yaml` with `portal.url`, `machine_id`, `default_session`
2. Set up reverse SSH tunnel: `ssh -R 8765:localhost:8765 dev@138.197.145.5`
3. Renamed tmux session to `eric@eric-devbox`
4. Agentwire v1.1.0 installed via uv

**Local config fix:**
- Changed `tts.url` from `http://192.168.2.108:8100` to `http://localhost:8100` (use tunnel)

## Current Working Workaround

Pass `session="agentwire"` explicitly to route audio to the main browser:

```python
# In MCP calls from remote agents
agentwire_say(text="...", session="agentwire")
```

```bash
# CLI equivalent
agentwire say -s agentwire "text"
```

## Test Commands

```bash
# Test from remote WITH explicit session (works)
ssh dev@134.122.35.134 "~/.local/bin/agentwire say -s 'agentwire' 'test from jordan'"
ssh dev@138.197.145.5 "~/.local/bin/agentwire say -s 'agentwire' 'test from eric'"

# Test without explicit session (currently fails - needs fix)
ssh dev@134.122.35.134 "~/.local/bin/agentwire say 'test'"
```
