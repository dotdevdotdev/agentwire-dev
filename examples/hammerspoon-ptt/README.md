# Hammerspoon — toggle PTT + voice target-picker

A reference [Hammerspoon](https://www.hammerspoon.org/) config for AgentWire voice input on macOS. Unlike the basic hold-to-record setup ([`docs/wiki/communication/hammerspoon.md`](../../docs/wiki/communication/hammerspoon.md)), this variant is **toggle-based** (tap to start, tap to stop) and adds a **voice target-picker**: say a session name and it fuzzy-matches against your live sessions.

The whole thing is in [`init.lua`](init.lua) — copy it to `~/.hammerspoon/init.lua` (or `require` it) and reload.

## Prerequisites

1. `brew install --cask hammerspoon`
2. AgentWire on PATH (`~/.local/bin/agentwire`)
3. A **custom STT shim** running: `agentwire stt start` with `stt.backend: custom` in `~/.agentwire/config.yaml`. The host CLI records on the host, so it can't use the browser-tier recognizer — see [`docs/wiki/voice/stt-self-hosted.md`](../../docs/wiki/voice/stt-self-hosted.md) and [`shim-contract.md`](../../docs/wiki/voice/shim-contract.md).
4. `hs.ipc` (the `hs` CLI) — the script `require`s it; first load may prompt to install.

## Hotkeys

| Chord | Action |
|-------|--------|
| **⌃⌥⌘ Space** | Toggle record → send transcript to the target session |
| **⌃⌥⌘ T** | Toggle record → type transcript at the cursor (dictation, any app) |
| **⌃⌥⌘ S** | Toggle record → fuzzy-pick the target session by voice |

Tap once to start recording, tap the **same** key to stop. The hyper chord (`ctrl+alt+cmd`) is collision-free with normal app shortcuts; change `HYPER` at the top of `init.lua` to taste.

## How the voice target-picker works

This is the part that consumes `agentwire listen stop --stdout` — the transcript source merged on `main`:

1. **⌃⌥⌘S** → `agentwire listen start` (alert: "Say a session name…").
2. **⌃⌥⌘S** again → `agentwire listen stop --stdout`. Unlike `-s <session>` or `--type`, `--stdout` doesn't paste or send anywhere — it prints the raw transcript to stdout, which the script captures.
3. `agentwire list --sessions --json` → the live session list.
4. Character-level fuzzy match (below) picks the best-scoring session.
5. The script opens an `hs.chooser` and **auto-confirms** the winner; if no candidate clears the confidence threshold it leaves the chooser open for you to pick or type.

## The three gotchas (baked into `init.lua`)

These cost real time to rediscover, so they're called out inline in the code:

1. **`hs.chooser:choices()` is a setter, not a getter.** Calling it with no args *clears* the list. Keep your own `choices` table and fuzzy-match against that — only ever pass the list *into* `chooser:choices(choices)`.

2. **`hs.chooser:select(row)` fires the completion callback AND closes the chooser.** That's the mechanism for auto-confirm: compute the best fuzzy row, call `chooser:select(bestRow)`, and the completion callback sets `targetSession` with no click required.

3. **Character-level fuzzy match (Levenshtein + per-word containment bonus).** STT mangles short jargon names — "agentwire-dev" comes back as "agent wire dev". A prefix/equality test misses that. We score `1 − (editDistance / maxLen)` and add `+0.5` for each spoken word contained in the candidate; highest combined score wins. Verified mappings: `agent wire dev → agentwire-dev`, `my project → myproject`, `web site → website`.

## Troubleshooting

| Problem | Check |
|---------|-------|
| Alert shows but no transcript | `agentwire stt status`; confirm `stt.backend: custom` |
| Picker shows "No sessions running" | `agentwire list --sessions` from a normal shell |
| Wrong session picked | Lower/raise `MATCH_THRESHOLD`; below it the chooser stays open for manual pick |
| `agentwire: not found` | Fix the `agentwire` path or `PATH` at the top of `init.lua` |
| Debug the CLI side | `tail -f /tmp/agentwire-listen.log` |
