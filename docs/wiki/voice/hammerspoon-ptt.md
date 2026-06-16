# Hammerspoon — toggle PTT + voice target-picker

> Reference example. The runnable config and full setup live in
> [`examples/hammerspoon-ptt/`](../../../examples/hammerspoon-ptt/) —
> [`init.lua`](../../../examples/hammerspoon-ptt/init.lua) +
> [README](../../../examples/hammerspoon-ptt/README.md).

A more capable [Hammerspoon](https://www.hammerspoon.org/) config than the basic hold-to-record setup in [`communication/hammerspoon.md`](../communication/hammerspoon.md). This one is **toggle-based** (tap to start, tap to stop) and adds a **voice target-picker**: say a session name and it fuzzy-matches against your live sessions, then auto-confirms.

It's the canonical consumer of **`agentwire listen stop --stdout`** — the form that prints the raw transcript to stdout (no paste, no tmux send) so an external caller can do its own thing with the text.

## What it does

| Chord | Action | CLI under the hood |
|-------|--------|--------------------|
| ⌃⌥⌘ Space | Toggle record → send to target session | `listen start` … `listen stop -s <session>` |
| ⌃⌥⌘ T | Toggle record → type at cursor | `listen start` … `listen stop --type` |
| ⌃⌥⌘ S | Toggle record → pick target session by voice | `listen start` … `listen stop --stdout` + `list --sessions --json` |

The picker flow: record a phrase → `listen stop --stdout` captures the transcript → `list --sessions --json` gives the candidates → character-level fuzzy match picks the winner → `hs.chooser:select(row)` auto-confirms it.

## Prerequisites

- `brew install --cask hammerspoon`, AgentWire on PATH, and a **custom STT shim** running (`agentwire stt start`, `stt.backend: custom`). `agentwire listen` records on the host, so it can't use the browser recognizer — see [`stt-self-hosted.md`](stt-self-hosted.md) and [`shim-contract.md`](shim-contract.md).

## The three gotchas

These are the non-obvious bits that the example bakes in as inline comments:

1. **`hs.chooser:choices()` is a setter, not a getter** — calling it with no args clears the list. Keep your own `choices` variable and fuzzy-match against that.
2. **`hs.chooser:select(row)` fires the completion callback *and* closes the chooser** — use it to auto-confirm the fuzzy winner without a click.
3. **Character-level fuzzy match (Levenshtein + per-word containment bonus)** — STT turns "agentwire-dev" into "agent wire dev"; a prefix/equality test misses it, so score edit-distance similarity plus a bonus per spoken word contained in the candidate.

Full annotated code and troubleshooting: [`examples/hammerspoon-ptt/`](../../../examples/hammerspoon-ptt/).
