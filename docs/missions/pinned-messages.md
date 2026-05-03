> Living document. Update this, don't create new versions.

# Mission: Pinnable Messages

Let users "pin" any message in the portal so its content lifts into a floating, draggable, resizable panel that stays on top across typing, scrolling, and session switches.

**Tracking issue:** [#156](https://github.com/dotdevdotdev/agentwire-dev/issues/156)
**Status:** not started
**Depends on:** nothing
**Blocks:** nothing

## What we're shipping

| Behavior | Detail |
|---|---|
| Pin action on a message | Per-message pin button (or context menu) on each rendered message |
| Floating panel | Message content lifts into a new WinBox window |
| Draggable + resizable | Standard WinBox drag/resize affordances |
| Always on top | WinBox `top: true` so the pin stays visible while user types / scrolls |
| Survives session switching | Pin is parented to the desktop, not the session window |
| Survives reload | Pin state persisted to `localStorage`, restored on portal init |
| Unpin | Closing the WinBox removes the pin from `localStorage` |

## What this is NOT

- Not a server-side feature. No new endpoints, no DB changes, no MCP tools.
- Not multi-user / shared. Pins live in the user's browser only.
- Not a rich editor. Content is read-only — we copy the message text + role + timestamp into the pin and display it as-is.

## Where the work lands

| File | Change |
|---|---|
| `agentwire/static/js/windows/chat-window.js` | Add pin button to each `.chat-message` rendering inside `_addMessage()` (line ~441). Wire click → `pinManager.pin({ sessionId, role, text, timestamp })` |
| `agentwire/static/js/pinned-message.js` *(new)* | `PinnedMessage` class wrapping a WinBox instance. Same shape as `artifact-window.js` (constructor takes id, text, position; creates WinBox with `top: true`; reports close). |
| `agentwire/static/js/pin-manager.js` *(new)* | Singleton that owns the pin registry + `localStorage` IO. Methods: `pin(message)`, `unpin(id)`, `restoreAll()`. Storage key: `agentwire.pinned-messages`. |
| `agentwire/static/js/desktop.js` | On boot, call `pinManager.restoreAll()` so pinned panels reappear after reload. |
| `agentwire/static/css/desktop.css` | Style the pin button (visible on message hover) and the `.pinned-message` WinBox content (padding, role badge, timestamp). |

## Data shape

`localStorage["agentwire.pinned-messages"]` holds an array:

```json
[
  {
    "id": "pin-1714780000-abc",
    "sessionId": "main",
    "role": "assistant",
    "text": "<the message body>",
    "timestamp": "2026-05-03T15:46:40.000Z",
    "x": 240,
    "y": 120,
    "width": 380,
    "height": 220
  }
]
```

Position + size update on WinBox drag/resize end so the pin restores where the user left it.

## Edge cases to handle

- **Long messages** — pin body needs `overflow-y: auto`. Cap initial height; user can resize.
- **Markdown / code blocks** — chat messages today render via `_escapeHtml(text)` (plain text). Pin reuses the same plain-text rendering — no markdown escape mismatch risk.
- **Stale `localStorage`** — if a stored pin's `sessionId` no longer exists, the pin still restores; it's just a quote, not a live link.
- **Many pins** — soft cap of, say, 20. Beyond that, oldest gets dropped on `pin()` to bound `localStorage` size.
- **Multiple tabs** — each tab gets its own pins. No cross-tab sync (out of scope; would need `storage` event listener).

## Success criteria

- [ ] Pin button visible on hover for every message in chat-window
- [ ] Clicking pin opens a floating WinBox with the message text + role + timestamp
- [ ] Pin stays on top while typing in input box and scrolling chat history
- [ ] Switching sessions does not close pinned panels
- [ ] Reloading the portal restores pinned panels at their last position + size
- [ ] Closing the WinBox removes the pin from `localStorage`
- [ ] No console errors with 0, 1, 5, or 20 active pins

## Out of scope (could be follow-ups)

- Cross-tab pin sync via `storage` event
- Pinning from `session-window.js` (the monitor/terminal output) — text there is a continuous PTY stream, not discrete messages, so the UX shape differs
- Server-side pin storage (multi-device sync)
- Rich content (markdown, syntax-highlighted code) inside pins
- Pin grouping / labels

## Effort estimate

Roughly 3-5 hours of focused frontend work. WinBox is already bundled (used by `artifact-window.js`), which removes the largest unknown.
