# Damage Control: Security Firewall for AgentWire

> Living document. Update this, don't create new versions.

---

## Overview

Damage Control is a security firewall system that protects AgentWire from dangerous operations during parallel agent execution. It intercepts tool calls (Bash, Edit, Write) via PreToolUse hooks and blocks operations matching security patterns.

**Why Critical for AgentWire**: Parallel remote agent execution multiplies risk. A single `rm -rf /` in a remote session is unrecoverable. Multi-agent missions amplify the chance of catastrophic mistakes.

### Protection Layers

| Layer | Coverage |
|-------|----------|
| **Bash Tool** | Commands: `rm -rf`, `git push --force`, `systemctl stop`, database drops |
| **Edit Tool** | File protections: SSH keys, credentials, `.env` files, system configs |
| **Write Tool** | Same as Edit tool (creation protection) |
| **Audit Logging** | All security decisions logged for analysis and debugging |

---

## Architecture

```
AgentWire Session
    ↓
Tool Call (Bash/Edit/Write)
    ↓
PreToolUse Hook
    ↓
Damage Control Hook Script (Python/UV)
    ↓
rules/*.yaml → Check command/path
    ↓
Decision: Block (exit 2) | Allow (exit 0) | Ask (JSON response)
    ↓
[If blocked] Error message to agent
[If allowed] Command executes
[If ask] User prompt for confirmation
```

### File Structure

Hooks ship inside the `agentwire` package — Claude Code's `settings.json` invokes them directly via `uv run`:

```
agentwire/hooks/damage-control/       # Bundled in package
├── bash-tool-damage-control.py       # Bash tool hook
├── edit-tool-damage-control.py       # Edit tool hook
├── write-tool-damage-control.py      # Write tool hook
├── audit_logger.py                   # Audit logging framework
└── rules/                            # Pattern files (categorized)
    ├── core.yaml                     # rm, chmod, system-level dangers
    ├── git.yaml                      # force push, reset --hard
    ├── databases.yaml                # DROP, TRUNCATE
    ├── containers.yaml               # docker prune, etc.
    ├── cloud-hosting.yaml, aws.yaml, gcp.yaml, firebase.yaml
    ├── infrastructure.yaml, remote.yaml
    ├── agentwire.yaml                # tmux/session protections
    └── gws.yaml                      # Google Workspace CLI

~/.agentwire/
├── damage-control/                   # OPTIONAL user override — same shape as rules/
│   └── *.yaml                        # If present, replaces bundled rules wholesale
└── logs/
    └── damage-control/
        └── YYYY-MM-DD.jsonl          # Daily audit logs (audit_logger.py)
```

Hooks load every `*.yaml` file in the rules directory and merge their pattern lists.

---

## Security Patterns

Patterns live in **categorized YAML files** under `agentwire/hooks/damage-control/rules/` (12 files, one per topic). To override or extend, drop YAML files into `~/.agentwire/damage-control/` — when that directory exists with `*.yaml` files, hooks load from there instead of the bundled rules.

### Pattern Types

#### 1. bashToolPatterns (Bash commands)

Block dangerous shell commands using regex patterns:

```yaml
bashToolPatterns:
  - pattern: '\brm\s+(-[^\s]*)*-[rRf]'
    reason: rm with recursive or force flags

  - pattern: '\bgit\s+push\s+--force\b'
    reason: git push --force (use --force-with-lease)

  - pattern: '\bsystemctl\s+stop\b'
    reason: stopping system services
```

**Coverage**:
- Destructive file operations (`rm -rf`, `shred`, `truncate`)
- Permission changes (`chmod 777`, `chown root`)
- Git destructive operations (`reset --hard`, `push --force`)
- Database operations (`DROP DATABASE`, `TRUNCATE`)
- System operations (`shutdown`, `reboot`, `systemctl stop`)
- Docker destructive operations (`system prune`, `rm -v /`)
- Package manager risks (`apt-get autoremove`, `npm uninstall -g`)

#### 2. zeroAccessPaths (Complete blocks)

Paths that cannot be accessed at all (read, write, edit, delete):

```yaml
zeroAccessPaths:
  - ~/.ssh/id_rsa
  - ~/.ssh/id_ed25519
  - ~/.agentwire/credentials/
  - ~/.agentwire/api-keys/
  - "*.pem"
  - "*.key"
  - ".env*"
```

Supports:
- Literal paths: `~/.ssh/id_rsa`
- Directory prefixes: `~/.agentwire/credentials/`
- Glob patterns: `*.pem`, `.env*`

#### 3. readOnlyPaths (No modifications)

Paths that can be read but not modified:

```yaml
readOnlyPaths:
  - ~/.agentwire/damage-control/
  - ~/.gitconfig
  - /etc/hosts
```

Blocks: write, append, edit, move, copy, delete, chmod, truncate

#### 4. noDeletePaths (Deletion protection)

Paths that can be modified but not deleted:

```yaml
noDeletePaths:
  - ~/.agentwire/sessions/
  - ~/.agentwire/missions/
  - .agentwire/mission.md
```

Blocks: `rm`, `unlink`, `rmdir`, `shred`

#### 5. allowedPaths (Granular path-based allowlist)

Paths where path-based protections (zeroAccess, readOnly, noDelete) are bypassed. Each entry specifies which operations are permitted. Hard-blocked bash patterns (like `rm -rf`) are **NEVER** bypassed. Bypassable bash patterns (like plain `rm`) can be overridden if the target path has the required operation permission.

**Operations**: `all`, `read`, `write`, `edit`, `delete`, `move`, `chmod`

**Global** (in any rules YAML — bundled or override):
```yaml
allowedPaths:
  - path: "*/dist/*"
    allow: all                     # bypass everything including bypassable rm
  - path: "~/.agentwire/.env"
    allow: [read, write, edit]     # but NOT delete
  - path: "*/__pycache__/*"
    allow: all
```

**Per-project** (in `.agentwire.yml`):
```yaml
safety:
  allowed_paths:
    - path: ".env.development"
      allow: [read, write, edit]
    - path: "dist/*"
      allow: all
```

Plain strings (legacy format) are auto-coerced to `{path: str, allow: all}` for backwards compatibility.

Per-project paths are relative to the project root and resolved to absolute paths before matching.

**Bypassable bash patterns**: Some bash patterns (plain `rm`, `rmdir`, `trash`) are marked `bypassable: true` in their rules YAML. When a command matches a bypassable pattern, the system checks if ALL target paths have the required operation permission (e.g., `delete` for `rm`). If all paths match, the command is allowed. Hard-blocked patterns (like `rm -rf`) are never bypassed regardless of permissions.

**Security**: When checking bypassable patterns, ALL paths in the command must have the required permission. A command like `rm /tmp/safe.txt /etc/passwd` is blocked because `/etc/passwd` is not in the allowlist, even though `/tmp/` has delete permission.

**Precedence**:
1. Hard-blocked `bashToolPatterns` (no `bypassable` flag) — always blocked, NEVER bypassed
2. Ask patterns (`ask: true`) — always prompt for confirmation
3. Bypassable `bashToolPatterns` (`bypassable: true`) — check allowlist for required operation
4. `allowedPaths` (global + per-project merged) — if target matches with correct operation, skip path checks
5. `zeroAccessPaths` — block (unless allowlisted with `read`)
6. `readOnlyPaths` — block modifications (unless allowlisted with specific operation)
7. `noDeletePaths` — block deletions (unless allowlisted with `delete`)

---

## AgentWire-Specific Protections

### Tmux Session Protection

```yaml
bashToolPatterns:
  - pattern: '\btmux\s+kill-server\b'
    reason: tmux kill-server (kills all sessions)

  - pattern: '\btmux\s+kill-session\s+-t\s+agentwire-'
    reason: killing AgentWire tmux sessions
```

Protects:
- `tmux kill-server` - would kill all sessions
- `tmux kill-session -t agentwire-*` - would kill AgentWire workers
- Allows: `tmux list-sessions`, `tmux attach`, killing non-AgentWire sessions

### Session File Protection

```yaml
zeroAccessPaths:
  - ~/.agentwire/credentials/
  - ~/.agentwire/api-keys/
  - ~/.agentwire/secrets/

noDeletePaths:
  - ~/.agentwire/sessions/
  - ~/.agentwire/missions/
  - .agentwire/mission.md
```

Protects:
- Credentials and API keys from any access
- Session state from deletion
- Mission files from accidental removal

### Remote Execution Safeguards

```yaml
bashToolPatterns:
  - pattern: '\bssh\s+[^\s]+\s+.*\brm\s+-[rf]'
    reason: dangerous remote rm command

  - pattern: '\bssh\s+[^\s]+\s+.*\bDROP\s+DATABASE\b'
    reason: remote database drop

  - pattern: '\bssh\s+[^\s]+\s+.*\bsystemctl\s+stop\b'
    reason: remote service shutdown
```

Protects against:
- Remote file deletions via SSH
- Remote database drops
- Remote service shutdowns
- Remote Docker prune operations

---

## Usage

### Testing Commands

Test commands before running them using the CLI:

```bash
# Test if command would be blocked
agentwire safety check "rm -rf /tmp"
# → ✗ Decision: BLOCK (rm with recursive or force flags)

# Test if command would be allowed
agentwire safety check "ls -la"
# → ✓ Decision: ALLOW

# Check overall safety status
agentwire safety status
# → Shows pattern counts, recent blocks, audit log location
```

### Querying Audit Logs

View security decisions from audit logs:

```bash
# Show recent blocked operations
agentwire safety logs --tail 20

# Show today's operations
agentwire safety logs --today

# Show blocks for specific session
agentwire safety logs --session mission/auth-refactor

# Search for specific pattern
agentwire safety logs --pattern "rm -rf"
```

**Audit Log Format**:
```json
{
  "timestamp": "2026-04-30T13:45:22Z",
  "session_id": "mission/damage-control",
  "agent_id": "wave-2-task-1",
  "tool": "Bash",
  "command": "rm -rf /tmp/test",
  "decision": "blocked",
  "blocked_by": "bashToolPattern: rm with recursive flags",
  "pattern_matched": "\\brm\\s+-[rRf]"
}
```

---

## Customizing Patterns

### Adding New Patterns

Drop a YAML file into `~/.agentwire/damage-control/` (creates the user-override layer):

```yaml
# ~/.agentwire/damage-control/myapp.yaml
bashToolPatterns:
  - pattern: '\bmyapp\s+destroy\b'
    reason: myapp destroy command is dangerous

zeroAccessPaths:
  - /myapp/secrets/

readOnlyPaths:
  - /myapp/config/production.yaml
```

**Heads-up:** the user-override directory **replaces** the bundled rules wholesale — copy what you need from `agentwire/hooks/damage-control/rules/` if you want to extend rather than override.

**Pattern Tips**:
- Use `\b` for word boundaries: `\brm\b` matches `rm` but not `format`
- Use `\s+` for required whitespace: `git\s+push` matches `git push`
- Test patterns before deploying: `agentwire safety check "command"`
- Patterns are case-insensitive for Bash commands

### Temporarily Disabling Protection

**Option 1**: Comment out specific patterns in your override `*.yaml`:

```yaml
# Temporarily disabled for migration
# - pattern: '\bgit\s+push\s+--force\b'
#   reason: git push --force
```

**Option 2**: Remove the hook entry from Claude Code's `~/.claude/settings.json` (the file Claude Code reads, not `~/.agentwire/settings.json`).

**Warning**: Disabling protection removes safety nets. Re-enable as soon as the risky operation is complete.

---

## Troubleshooting

### Hook Not Blocking Expected Command

**Check using CLI**:
```bash
# Test the command
agentwire safety check "your command here"

# Check hook status
agentwire hooks status
```

**Verify hook is registered**:
```bash
cat ~/.claude/settings.json | grep damage-control
```

### False Positive (Safe Command Blocked)

**Identify the pattern**:
```bash
agentwire safety check "your command here"
# Shows which pattern matched
```

**Adjust the pattern** — copy the relevant rules file from `agentwire/hooks/damage-control/rules/` into `~/.agentwire/damage-control/` and edit there:
```yaml
# Before (too broad)
- pattern: '\brm\b'

# After (more specific)
- pattern: '\brm\s+(-[^\s]*)*-[rRf]'
```

### Hook Timeout

Hooks have a 5-second timeout. If your rule files are very large or patterns are complex, you may hit it.

**Solution**: Optimize regex patterns
```yaml
# Slow (backtracking)
- pattern: '.*rm.*-rf.*'

# Fast (specific)
- pattern: '\brm\s+.*-[rf]'
```

### Audit Logs Growing Too Large

Audit logs are stored in `~/.agentwire/logs/damage-control/`.

**Implement log rotation** (future enhancement):
```bash
# Manual cleanup (keep last 30 days)
find ~/.agentwire/logs/damage-control/ -name "*.jsonl" -mtime +30 -delete
```

---

## Testing

### Manual Testing

Test with real AgentWire session:

```bash
# Create AgentWire session
agentwire new -s test-session

# In session, try dangerous commands
rm -rf /tmp/test           # Should be blocked
tmux kill-server           # Should be blocked
ls -la                     # Should be allowed

# Check audit logs
agentwire safety logs --session test-session
```

---

## Performance

### Hook Overhead

Each tool call adds <100ms overhead for pattern checking:
- Load `rules/*.yaml`: ~10ms (cached after first load)
- Pattern matching: ~50ms for 300+ patterns
- Audit logging: ~10ms

**Total**: ~70-100ms per command

### Optimization Tips

1. **Pattern order**: Put most common patterns first
2. **Specific patterns**: Avoid `.*` wildcards that cause backtracking
3. **Compiled patterns**: Python's `re` module caches compiled patterns
4. **Audit logs**: Async logging reduces blocking time

---

## Security Model

### What Damage Control Protects Against

✅ **Accidental catastrophic commands**
- `rm -rf /` during parallel agent execution
- `DROP DATABASE production` in wrong terminal
- `chmod 777` on sensitive files

✅ **Pattern-based risks**
- Deleting AgentWire infrastructure
- Modifying credentials/keys
- Remote destructive operations

✅ **Multi-agent amplification**
- Parallel agents making same mistake
- Cascading failures across sessions

### What Damage Control Does NOT Protect Against

❌ **Intentional malicious activity**
- Attackers can bypass hook system
- Not a replacement for proper auth/permissions

❌ **Logic errors**
- Code bugs that cause data corruption
- Application-level mistakes

❌ **Supply chain attacks**
- Malicious dependencies
- Compromised packages

### Defense in Depth

Damage Control is ONE layer:
- **System permissions**: Run AgentWire as non-root
- **Backups**: Regular backups of critical data
- **Version control**: Git commits for code changes
- **Audit logs**: Track all operations
- **Damage Control**: Block catastrophic commands

---

## FAQ

### Q: Does this slow down AgentWire?

**A**: Minimally. Hooks add ~70-100ms per command, which is negligible compared to actual command execution time.

### Q: Can I customize patterns per session?

**A**: Not yet. Patterns are global — bundled `agentwire/hooks/damage-control/rules/*.yaml` plus an optional override at `~/.agentwire/damage-control/`. Per-session overrides are a future enhancement.

### Q: What if I need to run a blocked command?

**A**: Four options:
1. Add the path to `allowedPaths` in a user-override `*.yaml` under `~/.agentwire/damage-control/` (global) or to `safety.allowed_paths` in `.agentwire.yml` (per-project)
2. Use "ask" patterns (prompts for confirmation)
3. Temporarily comment out the pattern in your override YAML
4. Run command outside AgentWire session

### Q: Do hooks work in remote sessions?

**A**: Yes, if the remote machine has AgentWire installed with damage-control hooks configured.

### Q: How do I add patterns for my own tools?

**A**: Drop a YAML file into `~/.agentwire/damage-control/` (the user-override layer):

```yaml
# ~/.agentwire/damage-control/mytool.yaml
bashToolPatterns:
  - pattern: '\bmytool\s+dangerous-operation\b'
    reason: mytool dangerous operation blocked
```

Remember: when this directory exists, the bundled rules are **replaced**. Copy the bundled `*.yaml` files in if you want to extend rather than override.

### Q: Can hooks block malicious LLM behavior?

**A**: Only pattern-based risks. Sophisticated attacks that don't match patterns can bypass the system. Damage Control is for accident prevention, not malware defense.

### Q: Where are audit logs stored?

**A**: `~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl` (one file per day)

---

## Related Documentation

- `agentwire safety` — CLI surface for testing commands and viewing audit logs (`agentwire safety check ...`, `agentwire safety logs`).
- `agentwire/hooks/damage-control/rules/` — bundled pattern source-of-truth.
