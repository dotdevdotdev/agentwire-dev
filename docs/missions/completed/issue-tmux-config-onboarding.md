# Issue: Add tmux Configuration to Onboarding

## Summary

Users setting up agentwire on new machines often have suboptimal tmux configs (or none at all), leading to poor UX with mouse scrolling, copy/paste, and terminal interaction.

## Problem

- Default tmux has mouse disabled
- Users can't scroll up to see history
- Copy/paste behavior is confusing without proper config
- No guidance in agentwire docs or onboarding

## Recommended tmux Config

```bash
# Standard tmux config for agentwire development
# Works on Mac (iTerm2) and Ubuntu

# Enable mouse support (scroll, click panes, resize)
set -g mouse on

# Increase scrollback buffer
set -g history-limit 50000

# Start window numbering at 1
set -g base-index 1
setw -g pane-base-index 1

# Renumber windows when one is closed
set -g renumber-windows on

# Faster key repetition
set -s escape-time 0

# Enable true color support
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",xterm-256color:Tc"

# Use vi keys in copy mode
setw -g mode-keys vi

# Copy mode improvements
bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode-vi y send -X copy-selection-and-cancel

# Don't exit copy mode on mouse drag end
unbind -T copy-mode-vi MouseDragEnd1Pane

# Disable mouse click/drag actions (keep scroll working)
# Prevents accidental copy-mode entry when clicking
unbind -n MouseDown1Pane
unbind -n MouseDown1Status
unbind -n MouseDrag1Border
unbind -n MouseDrag1Pane
unbind -n DoubleClick1Pane
unbind -n TripleClick1Pane

# Easier pane splitting (prefix + | and -)
bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"

# Reload config with prefix + r
bind r source-file ~/.tmux.conf \; display "Config reloaded"

# Status bar - minimal and clean
set -g status-style bg=default,fg=white
set -g status-left "[#S] "
set -g status-right "%H:%M"
set -g status-left-length 30

# Active pane border
set -g pane-active-border-style fg=blue

# Note: In iTerm2, hold Option (Alt) to bypass tmux mouse and use native selection
```

## Proposed Solutions

### Option 1: Add to `agentwire init`

During onboarding, offer to install the recommended tmux config:

```
? Install recommended tmux config? (Y/n)
  - Enables mouse scrolling
  - Better copy/paste
  - 50k line history
```

If user has existing config, offer to merge or skip.

### Option 2: Add CLI command

```bash
agentwire tmux install   # Install recommended config
agentwire tmux show      # Show recommended config
agentwire tmux check     # Check current config for issues
```

### Option 3: Documentation only

Add to docs:
- `docs/tmux-config.md` - Recommended config with explanations
- Link from CLAUDE.md and onboarding

## Key User Tips to Document

1. **Mouse scrolling**: Works after config, scroll up to see history
2. **Native selection in iTerm2**: Hold Option (Alt) key to bypass tmux mouse
3. **Copy in tmux**: Enter copy mode with `prefix + [`, select with mouse or `v`, yank with `y`
4. **Reload config**: `prefix + r` or `tmux source-file ~/.tmux.conf`
5. **New sessions**: Config applies to new sessions automatically; existing sessions need reload

## Files to Update

- `agentwire/onboarding.py` - Add tmux config step
- `docs/tmux-config.md` - New doc with full config and tips
- `CLAUDE.md` - Link to tmux docs
- Consider bundling config in `agentwire/templates/tmux.conf`
