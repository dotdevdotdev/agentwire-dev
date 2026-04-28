# Issue: Add Recommended tmux Configuration

## Summary

Provide a recommended tmux configuration for agentwire users that displays useful session info and works cross-platform (macOS + Linux).

## Status Bar

```
[session] window_num    branch | ~/path/to/dir | CPU:15% RAM:51% 14:20
```

- Session name (green)
- Window number (cyan when active)
- Git branch (yellow)
- Working directory with `~` shorthand (cyan)
- CPU/RAM as integer percentages
- Time in 24h format

## Key Features

- **Top-positioned status bar** - keeps prompt at bottom where users expect it
- **Mouse scroll enabled** - navigate agent output history
- **50k line scrollback** - large buffer for long agent sessions
- **Vi copy mode** - `v` to select, `y` to yank
- **Click/drag disabled** - prevents accidental interactions with agent sessions
- **Cross-platform metrics** - auto-detects macOS (`top -l`, `memory_pressure`) vs Linux (`top -bn1`, `free`)

## Files to Add

1. `docs/tmux-config.md` - documentation and explanation
2. `docs/tmux.conf` - raw config file for copying

## Installation

```bash
cp docs/tmux.conf ~/.tmux.conf
tmux source-file ~/.tmux.conf
```

## Config

```bash
# Standard tmux config for agentwire development
# Works on Mac (iTerm2) and Ubuntu

set -g mouse on
set -g history-limit 50000
set -g base-index 1
setw -g pane-base-index 0
set -g renumber-windows on
set -s escape-time 0
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",xterm-256color:Tc"
setw -g mode-keys vi

bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode-vi y send -X copy-selection-and-cancel
unbind -T copy-mode-vi MouseDragEnd1Pane

unbind -n MouseDown1Pane
unbind -n MouseDown1Status
unbind -n MouseDrag1Border
unbind -n MouseDrag1Pane
unbind -n DoubleClick1Pane
unbind -n TripleClick1Pane

bind | split-window -h -c "#{pane_current_path}"
bind - split-window -v -c "#{pane_current_path}"
bind r source-file ~/.tmux.conf \; display "Config reloaded"

set -g status-position top
set -g status-style bg=default,fg=white
set -g status-interval 5
set -g status-left "#[fg=green][#S]#[fg=white] "
set -g status-left-length 30

setw -g window-status-format "#I"
setw -g window-status-current-format "#[fg=cyan,bold]#I#[fg=white,nobold]"

set -g status-right-length 100
set -g status-right "#[fg=yellow]#(cd '#{pane_current_path}' && git branch --show-current 2>/dev/null || echo '-')#[fg=white] | #[fg=cyan]#(echo '#{pane_current_path}' | sed 's|$HOME|~|')#[fg=white] | CPU:#(if [ $(uname) = Darwin ]; then top -l 1 -n 0 2>/dev/null | awk '/CPU usage/{print int($3)}'; else top -bn1 2>/dev/null | awk '/Cpu/{print int($2+$4)}'; fi)%% RAM:#(if [ $(uname) = Darwin ]; then memory_pressure 2>/dev/null | awk '/System-wide/{print int(100-$5)}'; else free 2>/dev/null | awk '/Mem:/{print int($3/$2*100)}'; fi)%% %H:%M"

set -g pane-active-border-style fg=blue
```

## Notes

- In iTerm2, hold Option to bypass tmux mouse and use native selection
- Status refreshes every 5 seconds to balance responsiveness with CPU usage
