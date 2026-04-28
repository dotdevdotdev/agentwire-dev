# Damage Control Migration Guide

> Living document. Update this, don't create new versions.

**For**: Existing AgentWire installations
**Status**: Active
**Last Updated**: 2026-01-05

---

## Overview

This guide walks you through enabling damage-control security hooks in an existing AgentWire installation. The process takes ~5-10 minutes and requires no downtime.

**What You'll Get**:
- Protection from catastrophic commands (`rm -rf /`, `DROP DATABASE`, etc.)
- File protections for SSH keys, credentials, `.env` files
- AgentWire-specific protections (tmux sessions, mission files)
- Audit logging of all security decisions
- Interactive testing tool

---

## Prerequisites

Before starting:

- ✅ AgentWire installed and working
- ✅ Python 3.8+ available
- ✅ UV package manager installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- ✅ Write access to `~/.agentwire/`

Check UV installation:
```bash
uv --version
# Should show: uv 0.x.x
```

---

## Installation Steps

### Step 1: Verify Current State

Check if damage-control is already installed:

```bash
ls -la ~/.agentwire/hooks/damage-control/
```

**If directory exists**: You may already have damage-control installed. Skip to [Step 5: Verify Installation](#step-5-verify-installation).

**If directory doesn't exist**: Continue with Step 2.

### Step 2: Create Hook Directory Structure

Create the damage-control directory:

```bash
mkdir -p ~/.agentwire/hooks/damage-control
mkdir -p ~/.agentwire/logs/damage-control
```

### Step 3: Copy Hook Files

You have two options:

#### Option A: Copy from AgentWire Repository (Recommended)

If you have the AgentWire source:

```bash
# Copy patterns
cp ~/projects/agentwire-dev/.agentwire-templates/hooks/damage-control/patterns.yaml \
   ~/.agentwire/hooks/damage-control/

# Copy hook scripts
cp ~/projects/agentwire-dev/.agentwire-templates/hooks/damage-control/*.py \
   ~/.agentwire/hooks/damage-control/
```

#### Option B: Copy from Claude Code Damage Control

If you have the claude-code-damage-control project:

```bash
# Copy patterns
cp ~/projects/claude-code-damage-control/.claude/skills/damage-control/patterns.yaml \
   ~/.agentwire/hooks/damage-control/

# Copy and adapt hook scripts (requires manual editing)
# See "Manual Installation" section below
```

### Step 4: Register Hooks in Settings

Edit or create `~/.agentwire/settings.json`:

```bash
# Create settings file if it doesn't exist
touch ~/.agentwire/settings.json
```

Add hook configuration:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "uv run ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py",
          "timeout": 5
        }]
      },
      {
        "matcher": "Edit",
        "hooks": [{
          "type": "command",
          "command": "uv run ~/.agentwire/hooks/damage-control/edit-tool-damage-control.py",
          "timeout": 5
        }]
      },
      {
        "matcher": "Write",
        "hooks": [{
          "type": "command",
          "command": "uv run ~/.agentwire/hooks/damage-control/write-tool-damage-control.py",
          "timeout": 5
        }]
      }
    ]
  }
}
```

**If settings.json already exists**: Merge the hooks configuration with existing content.

### Step 5: Verify Installation

Test that hooks are working:

```bash
cd ~/.agentwire/hooks/damage-control

# Test interactive mode
uv run test-damage-control.py -i
```

You should see:
```
============================================================
  AgentWire Damage Control Interactive Tester
============================================================
Config: /Users/[you]/.agentwire/hooks/damage-control/patterns.yaml
Loaded: 116 bash patterns, 44 zero-access, 43 read-only, 37 no-delete paths
```

**Test a dangerous command**:
```
Tool [1/2/3/q]> 1
Command> rm -rf /tmp/test

BLOCKED - 1 pattern(s) matched:
   - rm with recursive or force flags
```

**Test a safe command**:
```
Command> ls -la

ALLOWED - No dangerous patterns matched
```

If both tests work, installation is successful!

### Step 6: Test in Real Session

Start an AgentWire session and verify hooks are active:

```bash
# Start test session
agentwire new -s test-damage-control

# In session prompt, try:
# "Try to run: rm -rf /tmp/test"
```

The agent should receive an error message that the command is blocked.

---

## Manual Installation (Advanced)

If copying hook scripts from claude-code-damage-control, you'll need to adapt path resolution.

### Adapting Hook Scripts

Edit each hook script (bash-tool-damage-control.py, edit-tool-damage-control.py, write-tool-damage-control.py):

**Find this section**:
```python
def get_config_path() -> Path:
    """Get path to patterns.yaml."""
    # Claude Code specific
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        config = Path(project_dir) / ".claude" / "skills" / "damage-control" / "patterns.yaml"
        if config.exists():
            return config

    return Path(__file__).parent / "patterns.yaml"
```

**Replace with**:
```python
def get_config_path() -> Path:
    """Get path to patterns.yaml."""
    # AgentWire specific
    agentwire_dir = os.environ.get("AGENTWIRE_DIR", os.path.expanduser("~/.agentwire"))
    config = Path(agentwire_dir) / "hooks" / "damage-control" / "patterns.yaml"
    if config.exists():
        return config

    # Fallback to script directory
    return Path(__file__).parent / "patterns.yaml"
```

This adaptation makes hooks look in `~/.agentwire/hooks/damage-control/` instead of Claude Code locations.

---

## Customization for Your Environment

### Adding Project-Specific Patterns

Edit `~/.agentwire/hooks/damage-control/patterns.yaml` to add patterns for your tools:

```yaml
bashToolPatterns:
  # Your custom patterns
  - pattern: '\bmyapp\s+destroy\b'
    reason: myapp destroy is dangerous

  - pattern: '\bmyapp\s+reset\s+--production\b'
    reason: resetting production data

zeroAccessPaths:
  # Your sensitive paths
  - ~/myapp/secrets/
  - ~/myapp/credentials/

noDeletePaths:
  # Protect your important files
  - ~/myapp/data/
  - ~/myapp/backups/
```

**Test your patterns**:
```bash
cd ~/.agentwire/hooks/damage-control
uv run test-damage-control.py -i
```

### Adjusting Timeout

If you have many patterns or complex regex, you may need to increase timeout in `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "uv run ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py",
          "timeout": 10  // Increased from 5 to 10 seconds
        }]
      }
    ]
  }
}
```

### Disabling Specific Protections

Comment out patterns you don't need in `patterns.yaml`:

```yaml
bashToolPatterns:
  # Disabled - we use git push --force frequently
  # - pattern: '\bgit\s+push\s+--force\b'
  #   reason: git push --force

  # Keep this one
  - pattern: '\brm\s+-[rf]'
    reason: rm with recursive/force flags
```

---

## Rollback Instructions

If you need to disable damage-control:

### Option 1: Disable Hooks (Temporary)

Edit `~/.agentwire/settings.json` and comment out the hooks:

```json
{
  "hooks": {
    "PreToolUse": [
      // Temporarily disabled
      // {
      //   "matcher": "Bash",
      //   "hooks": [...]
      // }
    ]
  }
}
```

### Option 2: Complete Removal

```bash
# Remove hook files
rm -rf ~/.agentwire/hooks/damage-control/

# Remove hooks from settings.json
# Edit ~/.agentwire/settings.json manually

# Remove audit logs (optional)
rm -rf ~/.agentwire/logs/damage-control/
```

**Restart AgentWire** after removing hooks to ensure changes take effect.

---

## Troubleshooting

### "Hook timeout" Errors

**Symptom**: Commands hang or timeout.

**Solution 1**: Increase timeout in settings.json (see [Adjusting Timeout](#adjusting-timeout)).

**Solution 2**: Simplify complex patterns that cause backtracking:
```yaml
# Slow
- pattern: '.*rm.*-rf.*'

# Fast
- pattern: '\brm\s+.*-[rf]'
```

### False Positives

**Symptom**: Safe commands are blocked.

**Solution**: Identify the blocking pattern and make it more specific:

```bash
cd ~/.agentwire/hooks/damage-control
uv run test-damage-control.py -i

# Test your safe command to see which pattern blocks it
```

Edit `patterns.yaml` to refine the pattern.

### Hooks Not Working

**Check 1**: Verify settings.json syntax
```bash
cat ~/.agentwire/settings.json | python -m json.tool
# Should output valid JSON
```

**Check 2**: Verify hook scripts exist
```bash
ls -la ~/.agentwire/hooks/damage-control/*.py
```

**Check 3**: Test hook directly
```bash
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' | \
  uv run ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py

# Should exit with code 2 (blocked)
echo $?  # Should print: 2
```

**Check 4**: Verify UV is available
```bash
which uv
uv --version
```

### Patterns Not Loading

**Symptom**: Interactive test shows "0 patterns loaded".

**Solution**: Check patterns.yaml location and syntax:

```bash
# Check file exists
ls -la ~/.agentwire/hooks/damage-control/patterns.yaml

# Check YAML syntax
cat ~/.agentwire/hooks/damage-control/patterns.yaml | python -c "import yaml, sys; yaml.safe_load(sys.stdin)"
# Should complete without errors
```

---

## Migration Checklist

Use this checklist to track your migration:

- [ ] Verified UV is installed (`uv --version`)
- [ ] Created `~/.agentwire/hooks/damage-control/` directory
- [ ] Copied patterns.yaml to directory
- [ ] Copied hook scripts (bash, edit, write) to directory
- [ ] Created or updated `~/.agentwire/settings.json` with hook configuration
- [ ] Tested interactive mode (`uv run test-damage-control.py -i`)
- [ ] Tested dangerous command is blocked (e.g., `rm -rf /tmp/test`)
- [ ] Tested safe command is allowed (e.g., `ls -la`)
- [ ] Tested in real AgentWire session
- [ ] Verified audit logs are being created (`~/.agentwire/logs/damage-control/`)
- [ ] Added project-specific patterns (if needed)
- [ ] Documented customizations for team

---

## Best Practices

### After Installation

1. **Test thoroughly**: Run interactive tests with your common commands
2. **Monitor audit logs**: Review `~/.agentwire/logs/damage-control/` for false positives
3. **Customize patterns**: Add project-specific dangerous operations
4. **Document exceptions**: If you disable patterns, document why
5. **Update patterns**: Periodically review and add new protections

### For Teams

1. **Share patterns.yaml**: Commit to version control (without sensitive paths)
2. **Document overrides**: Track which patterns are disabled and why
3. **Review audit logs**: Periodically check for security decisions
4. **Onboard new members**: Include damage-control in onboarding docs

### Maintenance

1. **Monthly review**: Check audit logs for new dangerous patterns to add
2. **Pattern optimization**: Refine patterns that cause false positives
3. **Log rotation**: Archive old audit logs (>30 days)
4. **Upstream updates**: Pull pattern updates from AgentWire releases

---

## Advanced: Per-Machine Configuration

If you work on multiple machines with different security needs:

### Machine-Specific Patterns

Use environment variable to specify custom patterns:

```bash
# In ~/.bashrc or ~/.zshrc
export AGENTWIRE_DIR=~/.agentwire-workstation  # Different dir per machine
```

Then maintain separate patterns.yaml per machine:
```
~/.agentwire-workstation/hooks/damage-control/patterns.yaml  # Work machine
~/.agentwire-personal/hooks/damage-control/patterns.yaml     # Personal machine
```

### Shared Base + Local Overrides

Symlink shared patterns, override with local additions:

```bash
# Shared patterns (version controlled)
ln -s ~/projects/myteam/damage-control/patterns.yaml \
      ~/.agentwire/hooks/damage-control/patterns-base.yaml

# Local additions (not version controlled)
cat > ~/.agentwire/hooks/damage-control/patterns-local.yaml <<EOF
bashToolPatterns:
  - pattern: '\bmylocaltool\s+danger\b'
    reason: my local dangerous tool
EOF
```

Modify hook scripts to load both files (requires code changes).

---

## Getting Help

### Resources

- [Main Documentation](./damage-control.md) - Complete damage-control reference

### Common Issues

| Issue | Solution |
|-------|----------|
| Hook timeout | Increase timeout in settings.json |
| False positive | Refine pattern in patterns.yaml |
| Patterns not loading | Check YAML syntax, verify file path |
| Hooks not running | Verify settings.json registration |
| UV not found | Install UV: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

### Reporting Issues

If you encounter issues:

1. **Gather diagnostic info**:
   ```bash
   uv --version
   cat ~/.agentwire/settings.json
   ls -la ~/.agentwire/hooks/damage-control/
   ```

2. **Test hook directly**:
   ```bash
   echo '{"tool_name":"Bash","tool_input":{"command":"test"}}' | \
     uv run ~/.agentwire/hooks/damage-control/bash-tool-damage-control.py
   ```

3. **Check audit logs**:
   ```bash
   cat ~/.agentwire/logs/damage-control/*.jsonl | tail -20
   ```

4. **Open issue** with diagnostic output

---

## Migration Timeline

**Recommended approach**:

1. **Day 1**: Install on development machine, test thoroughly
2. **Week 1**: Monitor audit logs, refine patterns
3. **Week 2**: Roll out to team (1-2 people at a time)
4. **Month 1**: Evaluate effectiveness, adjust patterns
5. **Ongoing**: Maintain and update patterns

**Quick migration** (if confident):
1. Install hooks (15 minutes)
2. Test in staging session (30 minutes)
3. Deploy to all machines (1 hour)

---

## Success Criteria

Your migration is successful when:

- ✅ Interactive test tool works (shows loaded patterns)
- ✅ Dangerous command blocked in test: `rm -rf /tmp/test`
- ✅ Safe command allowed in test: `ls -la`
- ✅ Hooks work in real AgentWire session
- ✅ Audit logs created: `~/.agentwire/logs/damage-control/YYYY-MM-DD.jsonl`
- ✅ No false positives in your normal workflow
- ✅ Team trained on using damage-control

---

## Changelog

### 2026-01-05 - v1.0 Initial Release
- Initial migration guide
- Installation instructions
- Troubleshooting guide
- Best practices
