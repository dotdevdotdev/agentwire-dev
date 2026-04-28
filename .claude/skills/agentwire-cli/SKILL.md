---
name: agentwire-cli
description: Full `agentwire` CLI command reference — session/pane management, portal, TTS/STT, voice, channels (email/sms/webhook/telegram/discord/slack), machine/tunnel/lock management, projects/history/roles, scheduler, overnight queue, safety/diagnostics. Use when running or composing `agentwire ...` shell commands, building automation scripts, or answering "how do I X from the CLI".
---

# AgentWire CLI Reference

```bash
# Session management
agentwire new -s name           # not: tmux new-session
agentwire send -s name "prompt" # not: tmux send-keys
agentwire send-keys -s name key1 key2  # raw keys with pauses
agentwire output -s name        # not: tmux capture-pane
agentwire info -s name          # session metadata (cwd, panes) as JSON
agentwire kill -s name          # not: tmux kill-session
agentwire list                  # not: tmux list-sessions
agentwire recreate -s name      # destroy and recreate with fresh worktree
agentwire worktree name         # new branch + worktree + session
agentwire worktree name -b develop  # from specific base branch
agentwire worktree name -c      # from repo's current branch
agentwire worktree name -e      # checkout existing branch (no new branch)
agentwire worktree name --ref v2.0  # detached at tag/commit
agentwire fork -s name          # fork session into new worktree
agentwire fork -s name -t project/branch --commit abc123  # fork from specific commit

# Textual REPL (claude-agent-sdk surface — see docs/wiki/sessions/repl-tui.md)
agentwire repl                          # interactive Textual TUI (default)
agentwire repl --mode bypass|prompted|restricted   # permission mode
agentwire repl --model claude-sonnet-4-6           # override model
agentwire repl --view fanout --cols 3              # multi-generation A/B
agentwire repl --view fanout --cols 2 \\
  --col-model 0=claude-opus-4-7 --col-model 1=claude-sonnet-4-6  # compare models
agentwire repl --view fanout --cols 2 \\
  --col-effort 0=max --col-effort 1=high           # compare effort
agentwire repl -p "summarize foo.py"               # one-shot stdout pipe
# Inside the REPL:  /help /clear /cost /tools /model /save /resume <name>
#                   /effort <level> /thinking <mode> /say <text> /scrub
#                   /theme <name> /run-workflow <name> /exit
# Inside fanout: /exit, /quit, /cancel, /clear (also Ctrl+C, Ctrl+D)

# Pane commands (for workers within same session)
agentwire spawn --roles worker  # spawn worker pane
agentwire send --pane 1 "task"  # send to pane
agentwire output --pane 1       # read pane output
agentwire kill --pane 1         # kill pane
agentwire jump --pane 1         # focus pane
agentwire split -s name         # add terminal pane(s)
agentwire detach -s name        # move pane to its own session
agentwire resize -s name        # resize window to fit largest client

# Portal management
agentwire portal start          # start in tmux
agentwire portal stop           # stop portal
agentwire portal restart        # stop + start
agentwire portal status         # check health

# TTS/STT servers
agentwire tts start|stop|status # TTS server management
agentwire stt start|stop|status # STT server management

# Voice
agentwire say "text"            # speak (auto-routes to browser or local)
agentwire say -s name "text"    # speak to specific session
agentwire reply "text"           # reply to channel user (Discord/Slack/Telegram)
agentwire notify-parent "text"   # notify parent session (worker→orchestrator)
agentwire notify-parent --to name "text" # notify specific session
agentwire listen start|stop|cancel  # voice recording

# Voice cloning
agentwire voiceclone start      # start recording voice sample
agentwire voiceclone stop name  # stop and save as voice clone
agentwire voiceclone list       # list available voices
agentwire voiceclone delete name # delete a voice clone

# Artifact windows (agent visual canvas)
agentwire open <url> --title "T"  # open URL or local file as artifact window
agentwire open dashboard.html     # open from ~/.agentwire/artifacts/

# Channels (communication integrations)
agentwire channels list         # list all registered channels
agentwire channels list --json  # JSON output

# Email (send-only channel)
agentwire email --to addr --subject "Subject" --body "Body"
agentwire email --body "msg" # uses default_to from config
agentwire email --attach file.pdf --body "See attached"

# Quo SMS (send-only channel, no deps)
agentwire quo --body "msg" --to "+1234567890"

# SMS via Twilio (send-only channel, requires twilio)
agentwire sms --body "msg" --to "+1234567890"

# Webhook (send-only channel)
agentwire webhook --body "msg" --url "https://hooks.example.com"

# Telegram bridge (service channel)
agentwire telegram start       # start bot in tmux
agentwire telegram stop        # stop bot
agentwire telegram serve       # run bot in foreground
agentwire telegram status      # check bot status

# Discord bridge (service channel, requires discord.py)
agentwire discord start|serve|stop|status

# Slack bridge (service channel, requires slack-bolt)
agentwire slack start|serve|stop|status

# Machine management
agentwire machine list
agentwire machine add <id> --host <host> --user <user>
agentwire machine remove <id>

# SSH tunnels (for remote services)
agentwire tunnels up            # create all required tunnels
agentwire tunnels down          # tear down all tunnels
agentwire tunnels status        # show tunnel health
agentwire tunnels check         # verify tunnels are working

# Lock management (for scheduled tasks)
agentwire lock list             # list all locks
agentwire lock clean            # remove stale locks
agentwire lock remove <session> # force-remove a specific lock

# Project discovery
agentwire projects list         # discover projects from projects_dir
agentwire projects list --json  # JSON output for scripting

# Session history
agentwire history list          # list conversation history
agentwire history show <id>     # show session details
agentwire history resume <id>   # resume session (always forks)

# Roles management
agentwire roles list            # list available roles
agentwire roles show <name>     # show role details

# Scheduled workloads
agentwire ensure -s name --task task  # run named task reliably
agentwire task list [session]         # list tasks for session/project
agentwire task show session/task      # show task definition
agentwire task validate session/task  # validate task syntax

# Safety & diagnostics
agentwire safety check "cmd"    # test if command would be blocked
agentwire safety status         # show pattern counts and recent blocks
agentwire safety logs           # query audit logs
agentwire safety install        # install damage control hooks
agentwire hooks install         # install permission hook (Claude Code only)
agentwire hooks uninstall       # remove permission hook (Claude Code only)
agentwire hooks status          # check hook installation status
agentwire network status        # complete network health check
agentwire doctor                # auto-diagnose and fix issues

# Notifications
agentwire notify event          # notify portal of state changes (session/pane events)

# MCP Server
agentwire mcp                   # expose agentwire as MCP server

# Scheduler
agentwire scheduler start|serve|stop|status # manage scheduler daemon
agentwire scheduler board                   # show task board with overdue scores
agentwire scheduler live                    # show live scheduler state
agentwire scheduler events                  # show recent scheduler events
agentwire scheduler history                 # show recent run history
agentwire scheduler run task                # force-run a task now
agentwire scheduler enable|disable task     # enable/disable a task
agentwire scheduler report [--since 8h] [--artifact]  # generate morning report HTML
agentwire scheduler dashboard               # open scheduler dashboard

# Overnight session queue
agentwire overnight prepare --from <session> --task "desc"  # queue session
agentwire overnight list [--all]            # list queue items
agentwire overnight status                  # orchestrator state
agentwire overnight cancel <id>             # cancel item
agentwire overnight priority <id> <n>       # update priority
agentwire overnight start|serve|stop        # manage orchestrator daemon
agentwire overnight report                  # morning report

# Setup & Development
agentwire init                  # interactive setup wizard
agentwire generate-certs        # generate SSL certificates
agentwire dev                   # start/attach to dev session
agentwire rebuild               # clear uv cache and reinstall
agentwire uninstall             # uninstall the tool
```

Session formats: `name`, `project/branch` (worktree), `name@machine` (remote)
Pane targeting: `--pane N` auto-detects session from `$TMUX_PANE`

For CLI details: `agentwire --help` or `agentwire <cmd> --help`
