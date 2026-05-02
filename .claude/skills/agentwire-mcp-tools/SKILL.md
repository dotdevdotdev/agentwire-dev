---
name: agentwire-mcp-tools
description: Reference for the 90 `mcp__agentwire__*` MCP tools — session/pane management, voice/TTS, tasks/locks, channels (email/sms/webhook/discord/slack), machines/tunnels/network, history/roles/projects, scheduler, overnight queue, desktop UI, notifications. Use when agents inside agentwire sessions need to pick the right MCP tool instead of shelling out to the `agentwire` CLI.
---

# AgentWire MCP Tools

**Agents running in agentwire sessions should use MCP tools instead of CLI commands.** The agentwire MCP server provides tools that wrap CLI functionality. Use these instead of `Bash: agentwire <cmd>`.

## Session Management (9 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire list` | `sessions_list()` |
| `agentwire new -s name` | `session_create(name="...")` |
| `agentwire send -s name "msg"` | `session_send(session="...", message="...")` |
| `agentwire output -s name` | `session_output(session="...")` |
| `agentwire info -s name` | `session_info(session="...")` |
| `agentwire kill -s name` | `session_kill(session="...")` |
| `agentwire send-keys -s name key1 key2` | `session_send_keys(session="...", keys=["..."])` |
| `agentwire recreate -s name` | `session_recreate(session="...")` |
| `agentwire fork -s name -t project/branch` | `session_fork(session="...", target="...")` |
| `agentwire fork -s name -t project/branch --commit abc` | `session_fork(session="...", target="...", commit="abc")` |

## Pane Management (9 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire spawn --roles worker` | `pane_spawn(roles="worker")` |
| `agentwire send --pane 1 "msg"` | `pane_send(pane=1, message="...")` |
| `agentwire output --pane 1` | `pane_output(pane=1)` |
| `agentwire kill --pane 1` | `pane_kill(pane=1)` |
| `agentwire list` (in tmux) | `panes_list()` |
| `agentwire split -n 2` | `pane_split(count=2)` |
| `agentwire detach --pane 1 -s target` | `pane_detach(session="src", pane=1, target="target")` |
| `agentwire jump --pane 1` | `pane_jump(pane=1)` |
| `agentwire resize` | `pane_resize()` |

## Voice & TTS (12 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire say "text"` | `say(text="...")` |
| `agentwire reply "text"` | `reply(text="...")` |
| `agentwire notify-parent "text"` | `notify(text="...", to="...")` |
| `agentwire listen start` | `listen_start()` |
| `agentwire listen stop` | `listen_stop()` |
| `agentwire listen cancel` | `listen_cancel()` |
| `agentwire voiceclone start` | `voiceclone_start()` |
| `agentwire voiceclone stop name` | `voiceclone_stop(name="...")` |
| `agentwire voiceclone cancel` | `voiceclone_cancel()` |
| `agentwire voiceclone list` | `voiceclone_list()` |
| `agentwire voiceclone delete name` | `voiceclone_delete(name="...")` |
| (portal API) | `transcribe(audio_base64="...", format="webm")` |
| `agentwire voiceclone list` | `voices_list()` |

## Tasks & Locks (7 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire ensure -s x --task y` | `task_run(session="x", task="y")` |
| `agentwire task list x` | `task_list(session="x")` |
| `agentwire task show x/y` | `task_show(session="x", task="y")` |
| `agentwire task validate x/y` | `task_validate(session="x", task="y")` |
| `agentwire lock list` | `lock_list()` |
| `agentwire lock clean` | `lock_clean()` |
| `agentwire lock remove session` | `lock_remove(session="...")` |

## Operations (10 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire projects list` | `projects_list()` |
| `agentwire roles list` | `roles_list()` |
| `agentwire roles show name` | `role_show(name="...")` |
| `agentwire machine list` | `machines_list()` |
| `agentwire machine add id --host h --user u` | `machine_add(machine_id="...", host="...", user="...")` |
| `agentwire machine remove id` | `machine_remove(machine_id="...")` |
| `agentwire history list` | `history_list()` |
| `agentwire history show id` | `history_show(session_id="...")` |
| `agentwire history resume id -p path` | `history_resume(session_id="...", project="...")` |
| `agentwire handoff init --title hint` | `handoff_init(title="...")` |
| `agentwire handoff render <bundle-dir>` | `handoff_render(bundle_dir="...", story=True)` |
| `agentwire handoff list` | `handoff_list()` |
| `agentwire email --body "..." --to addr` | `email_send(body="...", to="...", attachments=["..."], plain_text=False)` |

## Channels (7 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire channels list` | `channels_list()` |
| `agentwire quo --body "..." --to "+1..."` | `quo_send(body="...", to="+1...")` |
| `agentwire sms --body "..." --to "+1..."` | `sms_send(body="...", to="+1...")` |
| `agentwire webhook --body "..." --url "..."` | `webhook_send(text="...", url="...")` |
| `agentwire discord status` | `discord_status()` |
| `agentwire slack status` | `slack_status()` |
| `agentwire email --body "..." --to addr` | `email_send(body="...", to="...", attachments=["..."])` |

## Notifications & Network (5 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire notify event` | `session_notify(event="...")` |
| `agentwire tunnels up` | `tunnels_up()` |
| `agentwire tunnels down` | `tunnels_down()` |
| `agentwire tunnels status` | `tunnels_status()` |
| `agentwire network status` | `network_status()` |

## Status (3 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire portal status` | `portal_status()` |
| `agentwire tts status` | `tts_status()` |
| `agentwire stt status` | `stt_status()` |

## Scheduler (8 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire scheduler status` | `scheduler_status()` |
| `agentwire scheduler board` | `scheduler_board()` |
| `agentwire scheduler live --json` | `scheduler_live()` |
| `agentwire scheduler events --json` | `scheduler_events(tail=20, task="")` |
| `agentwire scheduler run task` | `scheduler_run(task="...")` |
| `agentwire scheduler report --since 8h` | `scheduler_report(since="8h", artifact=False)` |
| `agentwire scheduler enable task` | `scheduler_enable(task="...")` |
| `agentwire scheduler disable task` | `scheduler_disable(task="...")` |
| `agentwire scheduler history` | `scheduler_history(limit=20)` |

## Overnight Session Queue (6 tools)

| CLI Command | MCP Tool |
|-------------|----------|
| `agentwire overnight prepare --from s --task d` | `overnight_prepare(session="...", description="...", priority=50)` |
| `agentwire overnight list` | `overnight_list()` |
| `agentwire overnight status` | `overnight_status()` |
| `agentwire overnight cancel id` | `overnight_cancel(item_id="...")` |
| `agentwire overnight priority id n` | `overnight_priority(item_id="...", priority=N)` |
| `agentwire overnight report` | `overnight_report()` |

## Desktop/Portal UI (10 tools)

| Action | MCP Tool |
|--------|----------|
| List open windows | `desktop_windows_list()` |
| Open session window | `desktop_open_session(session="...", mode="monitor")` |
| Open panel | `desktop_open_panel(panel_type="sessions")` |
| Open artifact window (URL/file) | `desktop_open_artifact(url="...", title="...")` |
| Write HTML + open as artifact | `desktop_write_artifact(filename="...", html_content="...", title="...")` |
| Post toast notification | `portal_notify(text="...", session="...", priority="normal")` |
| Close window | `desktop_close_window(window_id="...")` |
| Focus window | `desktop_focus_window(window_id="...")` |
| Tile window | `desktop_tile_window(window_id="...", zone="left")` |
| Minimize all | `desktop_minimize_all()` |
| Multi-window layout | `desktop_layout(windows=[{id: "...", zone: "left"}])` |

**90 tools total.** When to use CLI vs MCP:
- **MCP tools** — Agents in sessions (orchestrators, workers)
- **CLI commands** — Humans, shell scripts, automation outside of agent sessions

**Note:** MCP tools don't support git worktree creation. Workers spawned via `pane_spawn` share the orchestrator's working directory. For isolated commits with worktrees, use the CLI `agentwire spawn --branch <name>` directly.
