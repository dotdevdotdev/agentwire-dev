---
name: agentwire-cli
description: Full `agentwire` CLI command reference — session/pane management, portal, TTS/STT, voice, channels (email + quo, outbound-only), machine/tunnel/lock management, projects/history/roles, scheduler, overnight queue, web helper (fetch), safety/diagnostics. Use when running or composing `agentwire ...` shell commands, building automation scripts, or answering "how do I X from the CLI".
---

# AgentWire CLI Reference

```bash
# Session management
agentwire new -s name           # not: tmux new-session
agentwire new -s name --no-soul # skip the always-injected soul personality role
agentwire new -s name --first-message "idea"  # deliver first prompt once agent boots
                                #   (verified paste, local only; failure ≠ command failure)
agentwire send -s name "prompt" # not: tmux send-keys
agentwire send -s name --wait-ready --timeout 60 -- "prompt"
                                # wait for agent boot (banner + screen-stable +
                                #   trust-prompt auto-accept), verified delivery,
                                #   exit 1 if unverified; local only
agentwire send-keys -s name key1 key2  # raw keys with pauses
agentwire send-keys -s name --pane 2 key  # target a specific pane
agentwire new -s name --created-by orch  # record creator (prompt-routing parent);
                                #   default: calling tmux session; '' opts out
agentwire output -s name        # not: tmux capture-pane
agentwire info -s name          # session metadata (cwd, panes) as JSON
agentwire kill -s name          # not: tmux kill-session
agentwire list                  # not: tmux list-sessions
agentwire recreate -s name      # destroy and recreate with fresh worktree
agentwire worktree name         # new branch + worktree + STANDALONE session
                                #   "worktree session" ALWAYS means this command — never
                                #   `spawn --branch` (that makes a pane); add
                                #   --type claude-bypass for autonomous workers
                                #   Auto-injects the worktree-mission briefing role
                                #   (isolation, no rebuild/restart, verify in-worktree,
                                #   draft PR + notify-back) — first prompts only need
                                #   the task itself; --no-mission opts out
agentwire worktree name -b develop  # from specific base branch
agentwire worktree name -c      # from repo's current branch
agentwire worktree name -e      # checkout existing branch (no new branch)
agentwire worktree name --ref v2.0  # detached at tag/commit
agentwire fork -s name          # fork session into new worktree
agentwire fork -s name -t project/branch --commit abc123  # fork from specific commit

# Pane commands (worker PANES inside the current session — NOT worktree
# sessions; for parallel autonomous missions use `agentwire worktree` above)
agentwire spawn --roles worker  # spawn worker pane
agentwire spawn --branch name   # worker pane on an isolated worktree (still a pane)
agentwire send --pane 1 "task"  # send to pane
agentwire output --pane 1       # read pane output
agentwire kill --pane 1         # kill pane
agentwire jump --pane 1         # focus pane
agentwire split -s name         # add terminal pane(s)
agentwire detach -s name        # move pane to its own session
agentwire resize -s name        # resize window to fit largest client

# Boot everything
agentwire up                    # boot all services (portal, TTS, STT, scheduler,
                                #   custom) then start/attach the dev session
agentwire up --no-tts --no-stt  # skip optional voice services
agentwire up --dev              # run portal from source (uv run)

# Portal management
agentwire portal start          # start in tmux
agentwire portal stop           # stop portal
agentwire portal restart        # stop + start
agentwire portal status         # check health
agentwire portal token          # print the auth token (devices enter it once)
agentwire portal token --rotate # generate a new token (re-enter on devices)

# Scratch pad (shared notes — portal drawer Alt+N; file: ~/.agentwire/scratchpad.json)
agentwire scratchpad list       # list notes (newest first)
agentwire scratchpad add "text" --source mysession  # add a note (drawer refreshes live)
agentwire scratchpad remove <id> # delete a note
agentwire scratchpad clear      # delete all notes

# Custom services (registered long-running sessions — services.custom in config;
# autostart on portal launch, health-checked + restarted by the portal watchdog)
agentwire services list         # registry: autostart/restart/healthcheck per service
agentwire services status       # run healthchecks now (exit 1 if something's down)
agentwire services status name  # one service
agentwire services up <name>    # start (also clears 'down' state)
agentwire services up --all     # start all autostart services (skips downed)
agentwire services down <name>  # stop AND keep stopped (watchdog won't respawn)

# TTS/STT servers
agentwire tts start|stop|status # TTS server management
agentwire stt start|stop|status # STT server management

# Voice
agentwire say "text"            # speak (auto-routes to browser or local)
agentwire say -s name "text"    # speak to specific session
agentwire notify-parent "text"   # notify parent session (worker→orchestrator)
agentwire notify-parent --to name "text" # notify specific session
agentwire notify-parent --raw --to name "text"  # verbatim, no [NOTIFY ...] prefix
                                # (delivery is safety-gated: refuses targets showing a
                                #  live dialog / bare shells / parked sessions, verified paste)

# Prompt routing (interactive prompts → parent session; see wiki sessions/prompt-routing.md)
agentwire prompts status        # pending prompt markers
agentwire prompts tick          # run one sweep now (watchdog does this every 60s)
agentwire prompts answer -s name --pane 0 --expect <hash> 2  # guarded answer:
                                #   re-detects + hash-compares before sending keys —
                                #   NEVER answer dialogs with raw send-keys
agentwire prompts clear -s name --pane 1  # drop a marker

# Polite messaging (non-interrupting agent-to-agent inbox; see wiki sessions/messaging.md)
agentwire msg send --to name "text"          # queue a message (delivers when their box is clear)
agentwire msg send --to name --kind done "PR #312 drafted"  # kinds: note|done|request|escalation
agentwire msg send --to @all "team update"   # broadcast to live agent sessions except sender
agentwire msg inbox -s name                  # peek pending (does not drain)
agentwire msg flush -s name                  # attempt a drain now (still gated on empty box + safe target)
                                # `msg` NEVER clobbers a human's draft — unlike `send`, which
                                # pastes + Enter immediately. Use `send` only to forcibly drive a session now.

agentwire listen start|stop|cancel  # voice recording

# Voice cloning
agentwire voiceclone start      # start recording voice sample
agentwire voiceclone stop name  # stop and save as voice clone
agentwire voiceclone list       # list available voices
agentwire voiceclone delete name # delete a voice clone

# Artifact windows (agent visual canvas)
agentwire open <url> --title "T"  # open URL or local file as artifact window
agentwire open dashboard.html     # open from ~/.agentwire/artifacts/

# Channels (outbound notification integrations — email + quo)
agentwire channels list         # list all registered channels
agentwire channels list --json  # JSON output

# Email (send-only channel)
agentwire email --to addr --subject "Subject" --body "Body"
agentwire email --body "msg" # uses default_to from config
agentwire email --attach file.pdf --body "See attached"

# Quo SMS (send-only channel, no deps)
agentwire quo --body "msg" --to "+1234567890"

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
agentwire projects create name              # mkdir + minimal .agentwire.yml (claude-bypass)
                                            # (in git repos, .agentwire.yml is auto-added to
                                            #  .gitignore — personal config, keep it untracked)
agentwire projects create name --git-init   # also run `git init`
agentwire projects create name --from URL   # git clone URL instead of mkdir

# Session history
agentwire history list          # list conversation history
agentwire history show <id>     # show session details
agentwire history resume <id>   # resume session (always forks)

# Shareable conversation handoffs (issue #157)
agentwire handoff init [--title hint]      # create bundle dir + pre-filled ai-handoff.md template
agentwire handoff render <bundle-dir>      # render show-the-story.html from ai-handoff.md
agentwire handoff list                     # list past bundles
# Inside a Claude Code session, prefer the /handoff slash command — it walks the
# agent through filling the template using full conversation context (free, no
# fresh LLM call). Outputs land in ~/.agentwire/artifacts/handoff-<slug>/.

# Roles management
agentwire roles list            # list available roles
agentwire roles show <name>     # show role details

# Scheduled workloads
agentwire ensure -s name --task task  # run named task reliably
agentwire task list [session]         # list tasks for session/project
agentwire task show session/task      # show task definition
agentwire task validate session/task  # validate task syntax

# URL fetch (helper usable from any session, including pi)
agentwire fetch <url>                 # fetch a page via Jina Reader (markdown, JS-rendered)
agentwire fetch <url> --limit 4000    # cap chars (default 8000, 0 = no limit)

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

# Usage-limit recovery (deterministic watchdog, see docs/wiki/usage-limit-recovery.md)
agentwire limits tick           # one watchdog pass: sweep panes, resume what's due
agentwire limits status         # show sessions parked on usage limits
agentwire limits resume -s name [--force]  # manually resume a parked session now
agentwire limits install        # install + load the launchd watchdog (60s tick)
agentwire limits uninstall      # unload + remove the watchdog

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
agentwire up                    # boot all services + dev session (see "Boot everything")
agentwire dev                   # start/attach to dev session ONLY (no services)
agentwire rebuild               # clear uv cache and reinstall
agentwire uninstall             # uninstall the tool
```

`agentwire dev` only spawns the `agentwire` agent session — it does NOT start
the portal or any service. Use `agentwire up` after a reboot to bring up the
full stack. `up` brings up portal → TTS → STT → autostart custom services, then
runs `dev`; the scheduler rides along via the portal's `scheduler.autostart`.
TTS is skipped for `none`/`runpod` backends; STT is skipped without `stt.url`.

Session formats: `name`, `project/branch` (worktree), `name@machine` (remote)
Pane targeting: `--pane N` auto-detects session from `$TMUX_PANE`

For CLI details: `agentwire --help` or `agentwire <cmd> --help`
