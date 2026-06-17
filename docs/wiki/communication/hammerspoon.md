# Hammerspoon Push-to-Talk

Global hotkeys for voice input on macOS using [Hammerspoon](https://www.hammerspoon.org/). Two keys, both toggle-based (tap to start recording, tap again to stop + send) — works from any app.

- **⌥Space** — talk to the **tab target**: the session the portal is currently focused on (read from `~/.agentwire/active-session`, with a default fallback).
- **⌥⌘Space** — **pick-and-talk**: open a live session chooser *and* start recording at the same time; talk while you pick. You pick visually, so it can never misroute — there's no voice name-matching.

The runnable config lives at [`examples/hammerspoon-ptt/`](../../../examples/hammerspoon-ptt/) — [`init.lua`](../../../examples/hammerspoon-ptt/init.lua) + [README](../../../examples/hammerspoon-ptt/README.md). Copy `init.lua` to `~/.hammerspoon/init.lua` (or `require` it) and reload (Hammerspoon menu > Reload Config, or `⌘⇧R`).

## Prerequisites

1. **Hammerspoon** installed (`brew install --cask hammerspoon`)
2. **AgentWire** installed and on PATH (`~/.local/bin/agentwire`)
3. A **custom STT shim** running: `agentwire stt start` with `stt.backend: custom` in `~/.agentwire/config.yaml`. `agentwire listen` records on the host, so it can't use the browser-tier recognizer — see [`voice/stt-self-hosted.md`](../voice/stt-self-hosted.md) and [`voice/shim-contract.md`](../voice/shim-contract.md).
4. **IPC module** — Hammerspoon needs `hs.ipc` for the `hs` CLI. The config `require`s it; first load may prompt to install the CLI.

## Hotkeys

| Chord | Action |
|-------|--------|
| **⌥ Space** | Toggle record → send transcript to the **tab target** (focused portal session) |
| **⌥⌘ Space** | Toggle record + open chooser → send transcript to the **highlighted session** |

For **⌥⌘Space** you can finish three ways: press ⌥⌘Space again, press **Enter**, or **click** a row to send to the highlighted session; press **Esc** / dismiss to cancel with no send.

The CLI under the hood:

| Step | Command |
|------|---------|
| Begin recording | `agentwire listen start` |
| Stop + transcribe + send | `agentwire listen stop -s <session>` |
| Stop + discard (no send) | `agentwire listen cancel` |
| Live session list | `agentwire list --sessions --json` |

## How it works

### ⌥Space — voice follows the tab

The target is **re-read from `~/.agentwire/active-session` at stop time**, so it follows whichever portal tab you focused most recently. If the file is missing or empty it falls back to the `DEFAULT_TARGET` at the top of `init.lua` (`agentwire`). See [the active-session contract](#the-active-session-contract) below.

### ⌥⌘Space — pick-and-talk

The first press starts recording **and** opens an `hs.chooser` populated from `agentwire list --sessions --json` — so you talk while you scan the list. Filter by typing, move with the arrows, then finish (⌥⌘Space again / Enter / click) to send the utterance to the highlighted row. Because you select the destination visually, there's no fuzzy name-matching and no risk of misrouting.

### Single capture, idempotent finish

Only one recording is ever in flight. A small `mode` state machine (`nil` / `"tab"` / `"pick"`) guards it: while ⌥Space owns the capture, ⌥⌘Space is ignored and vice-versa. For the chooser, both the hotkey-again path and the chooser's own completion callback (Enter / click / Esc) can fire — the `mode` guard ensures exactly one of them sends, so you can never double-send.

### Safety auto-stop

A ~120s timer force-stops any capture left running (e.g. you walked away). For ⌥Space it sends to the current tab target; for ⌥⌘Space it cancels (never misroute on a timeout).

## The active-session contract

`~/.agentwire/active-session` is a one-line plain-text file holding the name of the session the portal desktop is currently focused on. It's how "voice follows the tab" works:

- **Writer (portal).** When a **session** window gains focus in the portal desktop, the frontend POSTs the session name to `POST /api/active-session` (auth-gated like every other `/api` route). The server writes the name to `~/.agentwire/active-session` with an atomic write (temp file + `os.replace`), so a reader never sees a half-written file. Only session windows update it — artifacts and panels don't change the voice target.
- **Reader (Hammerspoon ⌥Space).** Reads the first line, trims whitespace, and uses it as the `listen stop -s` target. Missing/empty → `DEFAULT_TARGET`.

Verify it by hand: focus different session windows in the portal and watch `cat ~/.agentwire/active-session` track the change.

## Gotchas (baked into `init.lua`)

1. **Stripped PATH.** Hammerspoon launches with `PATH=/usr/bin:/bin`, so the agentwire CLI and its child processes (ffmpeg, etc.) won't resolve. The config prepends `/opt/homebrew/bin:$HOME/.local/bin` to `PATH` for every shelled-out command.
2. **`hs.chooser:choices()` is a setter, not a getter.** Calling it with no args *clears* the list. The config only ever passes the list *into* `chooser:choices(choices)`.
3. **Idempotent finish.** See "Single capture, idempotent finish" above — the `mode` guard is what stops the hotkey-again and chooser-callback paths from both sending.

## Customization

```lua
-- top of init.lua
local DEFAULT_TARGET = "agentwire"   -- fallback when the shadow file is empty
local SAFETY_SECS = 120              -- force-stop a capture left running this long
local agentwire = os.getenv("HOME") .. "/.local/bin/agentwire"  -- CLI path
```

To rebind, edit the two `hs.hotkey.bind` calls at the bottom of `init.lua` (e.g. swap `{"alt"}` / `{"alt", "cmd"}` for another modifier set).

## Troubleshooting

| Problem | Check |
|---------|-------|
| No alert on key press | Hammerspoon running and config loaded? Console: `hs.alert.show("test")` |
| "Recording" shows but no transcript | `agentwire stt status`; confirm `stt.backend: custom` |
| ⌥Space sends to the wrong/default session | `cat ~/.agentwire/active-session`; focus a session window in the portal |
| Chooser shows "No sessions running" | `agentwire list --sessions` from a normal shell |
| `agentwire: not found` | Fix the `agentwire` path or `PATH` at the top of `init.lua` |
| Debug the CLI side | `tail -f /tmp/agentwire-listen.log` |
