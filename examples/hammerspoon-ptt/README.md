# Hammerspoon — two-key push-to-talk

A reference [Hammerspoon](https://www.hammerspoon.org/) config for AgentWire voice input on macOS. Two keys, both toggle-based (tap to start, tap to stop):

- **⌥Space** — talk to the **tab target**: the session the portal is currently focused on.
- **⌥⌘Space** — **pick-and-talk**: open a session chooser and start recording at the same time; talk while you pick.

The whole thing is in [`init.lua`](init.lua) — copy it to `~/.hammerspoon/init.lua` (or `require` it) and reload. The canonical write-up lives at [`docs/wiki/communication/hammerspoon.md`](../../docs/wiki/communication/hammerspoon.md).

## Prerequisites

1. `brew install --cask hammerspoon`
2. AgentWire on PATH (`~/.local/bin/agentwire`)
3. A **custom STT shim** running: `agentwire stt start` with `stt.backend: custom` in `~/.agentwire/config.yaml`. The host CLI records on the host, so it can't use the browser-tier recognizer — see [`docs/wiki/voice/stt-self-hosted.md`](../../docs/wiki/voice/stt-self-hosted.md) and [`shim-contract.md`](../../docs/wiki/voice/shim-contract.md).
4. `hs.ipc` (the `hs` CLI) — the script `require`s it; first load may prompt to install.

## Hotkeys

| Chord | Action |
|-------|--------|
| **⌥ Space** | Toggle record → send transcript to the **tab target** (focused portal session) |
| **⌥⌘ Space** | Toggle record + open chooser → send transcript to the **highlighted session** |

Tap once to start recording, tap the **same** key to stop. For ⌥⌘Space you can also press **Enter** / click a row to send, or **Esc** to cancel with no send.

## How it works

- **⌥Space** reads the target from `~/.agentwire/active-session` at *stop* time, so it follows whichever portal tab you last focused. Empty/missing file → falls back to the `DEFAULT_TARGET` at the top of `init.lua` (`agentwire`). The portal keeps that file current — see the active-session contract in the canonical doc.
- **⌥⌘Space** starts recording and opens an `hs.chooser` populated from `agentwire list --sessions --json`. You pick visually (type to filter / arrow / click), so it can **never misroute** — there's no voice name-matching. Second ⌥⌘Space press reads the highlighted row and sends there.

## Gotchas (baked into `init.lua`)

1. **Stripped PATH.** Hammerspoon launches with `PATH=/usr/bin:/bin`, so the CLI and its children (ffmpeg, etc.) won't resolve. The script prepends `/opt/homebrew/bin:$HOME/.local/bin`.
2. **`hs.chooser:choices()` is a setter, not a getter.** Calling it with no args *clears* the list. The script only ever passes the list *into* `chooser:choices(choices)`.
3. **Idempotent finish.** The hotkey-again path and the chooser's completion callback (Enter/click/Esc) can both fire — a `mode` guard ensures only one of them sends.

## Troubleshooting

| Problem | Check |
|---------|-------|
| Alert shows but no transcript | `agentwire stt status`; confirm `stt.backend: custom` |
| ⌥Space sends to the wrong/default session | `cat ~/.agentwire/active-session`; focus a session window in the portal |
| Chooser shows "No sessions running" | `agentwire list --sessions` from a normal shell |
| `agentwire: not found` | Fix the `agentwire` path or `PATH` at the top of `init.lua` |
| Debug the CLI side | `tail -f /tmp/agentwire-listen.log` |
