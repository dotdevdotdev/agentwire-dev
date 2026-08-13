---
name: agentwire-cli
description: Full `agentwire` CLI command reference — session/pane management, portal, TTS/STT, voice, channels (email + quo, outbound-only), machine/tunnel/lock management, projects/history/roles, scheduler, web helper (fetch), safety/diagnostics. Use when running or composing `agentwire ...` shell commands, building automation scripts, or answering "how do I X from the CLI".
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
agentwire new -s name --created-by orch  # FORCE this creator (prompt-routing parent)
                                #   regardless of project; '' forces standalone.
                                #   Default (no flag): inherit the calling tmux session
                                #   ONLY if -p/-s targets the caller's own project — a
                                #   different project defaults to a standalone root (#715)
agentwire output -s name        # not: tmux capture-pane
agentwire info -s name          # session metadata (cwd, panes) as JSON
agentwire kill -s name          # not: tmux kill-session
agentwire list                  # not: tmux list-sessions
agentwire recreate -s name      # destroy and recreate with fresh worktree
                                #   DESTRUCTIVE: rm -rf's the worktree dir + new branch
agentwire restart -s name       # relaunch IN PLACE, same conversation (#871) — /exit,
                                #   regenerate flags from the recorded roles/posture/model,
                                #   relaunch at the same cwd with --resume. Nothing on disk
                                #   is touched (unlike recreate) and no new tmux session is
                                #   made (unlike `history resume`). Works on a session that
                                #   ISN'T running — that's the post-reboot/post-rebuild case.
                                #   If the conversation's history is orphaned or gone it
                                #   starts FRESH with the role intact and says so.
                                #   Waits for the agent to come back (exit 1 if it doesn't);
                                #   --no-wait skips. Local only; can't restart itself.
agentwire wait --children       # BLOCK on the child sessions you spawned (#852) — collects
                                #   each report, tears the child down, names the ones that
                                #   never reported. Waiting idly instead gets YOU reaped
                                #   mid-fan-out (idle != done). --timeout bounds this call;
                                #   exit 1 = still pending, just call it again.
                                #   Enrollment is automatic on `new`/`worktree` (opt out with
                                #   --no-cohort; --kind orchestrator never enrolls).
agentwire worktree name         # new branch + worktree + STANDALONE session
                                #   "worktree session" ALWAYS means this command — never
                                #   `spawn --branch` (that makes a pane). Defaults to the
                                #   bypass posture (autonomous, topology-driven — same for
                                #   every role on this verb); override with --posture.
                                #   ROLE defaults to "worker" (zero behavior change) — the
                                #   worker-worktree etiquette role is intrinsic to that
                                #   default (isolation, no rebuild/restart, verify
                                #   in-worktree, draft PR + notify-back) — first prompts
                                #   only need the task itself
                                #   Base branch (default mode): --base wins, else config
                                #   worktree.default_base, else the repo's actual default
                                #   branch (origin/HEAD, fallback current) — no hardcoded 'main'.
                                #   --project defaults to the git root of cwd (monorepo-safe:
                                #   many sessions can target one repo from different branches).
                                #   config worktree.naming can template the NEW branch name.
agentwire worktree name -b develop  # from specific base branch
agentwire worktree name -c      # from repo's current branch
agentwire worktree name -e      # checkout existing branch (no new branch)
agentwire worktree name --ref v2.0  # detached at tag/commit
agentwire worktree name --prompt "task"  # spawn AND seed the first message in one call (verified delivery)
agentwire worktree name --kind orchestrator  # ROLE override (#716): a durable, replaceable-
                                #   persona project window instead of a safety-railed
                                #   subordinate. Roots by default (created_by='') unless
                                #   --created-by says otherwise.
agentwire orchestrator [name] -p <project>  # sugar for `worktree --kind orchestrator`
                                #   (name defaults to "orchestrator") — the durable-window
                                #   one-liner for a monorepo/large-repo project.
agentwire worktree name --kind reviewer  # ROLE override (#827): a PR-review station — safety-
                                #   railed the other way (never opens/merges its own PR,
                                #   iterates via msg_send, reports a verdict via notify_parent).
                                #   Stays parented like worker (not rooted). Pane/main topology
                                #   is the typical default (`agentwire new --kind reviewer`);
                                #   use `worktree --kind reviewer` for a local checkout to e2e
                                #   a sibling's branch.
agentwire worktree --list       # list this repo's worktree sessions + read-only git status; --all = every repo
agentwire worktree --status name  # read-only git status (dirty/ahead/behind/pushed) for one worktree
agentwire worktree --dangling    # LIVE worker sessions with an OPEN PR and no live recorded
                                #   parent (#716) — a PR nobody is positioned to review/merge.
                                #   Distinct from --list's "orphan" (dead session, disk remnant).
agentwire worktree --remove name  # ATOMIC teardown: kill session + force-remove worktree + delete
                                #   merged branch (local+remote) + unregister — fails loudly (non-zero,
                                #   registry entry kept) if the dir can't actually be cleared (#717).
                                #   --keep-branch skips branch cleanup; --force-delete-branch deletes
                                #   even if not confirmed merged (but refuses an OPEN PR — #756 — unless
                                #   --close-pr-branch is also given, since that would silently close it).
agentwire worktree --prune      # drop registry entries whose worktree is gone + git worktree prune
                                #   --gc-merged: also tear down (session+worktree+branch) any
                                #   still-present entry whose branch is confirmed merged
agentwire tabs track --session name --tab-id <id> [--url <url>]  # bookkeeping for a
                                #   claude-in-chrome tab the session opened, so worktree
                                #   teardown can report it if the session never closes it
agentwire tabs untrack --session name --tab-id <id>  # drop tracking after closing it yourself
agentwire tabs list [--session name]  # list tracked tabs (debug leaked verification tabs)
agentwire fork -s name          # fork session into new worktree
agentwire fork -s name -t project/branch --commit abc123  # fork from specific commit

# Helper session — a worker session with NO isolation (#838): shares the
# caller's checkout, so zero git work at creation (no worktree, no branch,
# nothing in the worktree registry). Reproduces a worker pane's one real
# advantage while keeping msg inbox / voice / prompt routing / portal
# visibility. `wait --children` collects its report then reaps it (topology
# "main"), unlike a worktree child which is left alive.
agentwire helper name           # worker session sharing this checkout
agentwire helper name -p ~/projects/repo --prompt "run the suite, report failures"
agentwire helper name --roles wiki  # extra roles STACK on worker + shared-checkout
                                #   The `shared-checkout` role is auto-injected:
                                #   read/edit freely, NEVER commit/branch/checkout/
                                #   stash/reset/pull — the checkout's owner commits.
                                #   Needs a commit or a PR? Use `worktree` instead.
                                #   Teardown is just `agentwire kill -s name`.

# Pane commands (worker PANES inside the current session — NOT worktree
# sessions; for parallel autonomous work use `agentwire worktree` above)
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
agentwire portal token          # print the bootstrap auth token (host/CLI/MCP credential)
agentwire portal token --rotate # generate a new bootstrap token (re-enter on devices)

# Per-device credentials (#423) — pair a device for its OWN revocable token
agentwire portal pair [--name phone]   # print a short-lived pairing code + QR → /pair?code=
agentwire portal devices [--json]      # list paired devices (id, name, last-seen, status)
agentwire portal revoke <id>           # revoke ONE device (others keep working)

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
agentwire stt start|stop|status # STT server management (host shim on :8101)
agentwire stt start             # engine from stt.engine (auto|moonshine|whisper); moonshine = fast CPU
agentwire stt start --backend moonshine --model moonshine/base --port 8101  # ad-hoc overrides

# Voice
agentwire say "text"            # speak (auto-routes to browser or local)
agentwire say -s name "text"    # speak to specific session
agentwire say "spoken" --display "richer card"  # speak AND show a desktop toast with different text (one call)
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
agentwire msg send --to name --kind done "PR #312 drafted"  # kinds: note|done|request|escalation|ingest|voice
agentwire msg send --to name --kind ingest --ref "/path/report.md" "topic"  # PASSIVE: never auto-delivered, pull-only
agentwire msg send --to @all "team update"   # broadcast to live agent sessions except sender
agentwire msg send --to name --body-file /tmp/body.md  # code-bearing body, no shell escaping ('-' = stdin)
agentwire msg inbox -s name                  # peek pending + passive (does not drain/consume)
agentwire msg pull -s name                   # read + REMOVE passive (ingest) messages — the voluntary pull
agentwire msg dead -s name                   # list dropped (dead-lettered) msgs + reason/timestamp
agentwire msg flush -s name                  # attempt a drain now (still gated on empty box + safe target)

# Research dropbox (Briefing Mode)
agentwire research dir -s name               # print the dropbox path (~/.agentwire/research/<session>/)
agentwire research ensure -s name            # create + print the dropbox path
                                # `msg` NEVER clobbers a human's draft — unlike `send`, which
                                # pastes + Enter immediately. Use `send` only to forcibly drive a session now.

agentwire listen start|stop|cancel  # host voice recording (needs stt.backend: custom + the :8101 shim)
agentwire listen stop -s name   # transcribe + send to a tmux session (default)
agentwire listen stop --type    # transcribe + type at cursor (Hammerspoon paste)
agentwire listen stop --stdout  # transcribe + print raw transcript to stdout, no paste/send
                                #   (scripting hook — Hammerspoon etc. capture $())

# Voices (custom/cloned voices come from a TTS shim — see docs/wiki/voice/shim-contract.md)
agentwire tts voices            # list available voices (custom-shim voices or Kokoro presets)

# Artifact windows (agent visual canvas)
agentwire open <url> --title "T"  # announce URL/local file as a click-to-open artifact notification (#817)
agentwire open dashboard.html     # announce from ~/.agentwire/artifacts/ — human clicks to open

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
agentwire projects create name              # mkdir + minimal .agentwire.yml (bypass)
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
agentwire doctor --voice        # only the push-to-talk path: mic, STT shim, portal/tunnel, tmux+PTT (pass/fail + fix)

# Notifications (the notify-* family, by target)
agentwire notify-event EVENT    # broadcast a portal lifecycle event (session/pane); usually called by tmux hooks
agentwire notify-parent "text"  # text up to your parent/orchestrator session
agentwire notify-user "text"    # desktop toast for the human (safe markdown: bold, [links](url), line breaks)

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

# Setup & Development
agentwire init                  # interactive setup wizard (ends on the portal URL)
agentwire init --assisted       # ...and spawn the Claude TTS/STT setup session at the end
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
TTS only starts a local service for the `custom` tier (the `default` tier uses browser/OS voice — no service); STT is skipped without `stt.url`.

Session formats: `name`, `project/branch` (worktree), `name@machine` (remote)
Pane targeting: `--pane N` auto-detects session from `$TMUX_PANE`

For CLI details: `agentwire --help` or `agentwire <cmd> --help`
