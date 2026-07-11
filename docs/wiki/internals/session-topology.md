# Session Topology (parent → child visualization)

> Living wiki. Update this page, don't create new versions.

Four mechanisms, shipped across #745–#749, that make the parent→child session tree visible and alive on the desktop instead of only existing in the sidebar's nested list:

| Mechanism | Module | Trigger |
|-----------|--------|---------|
| **Born-from-parent placement** | `static/js/spawn-ghost.js` (+ wiring in `desktop.js`) | A child session appears while its parent's window is open |
| **Connector overlay** | `static/js/topology-wires.js` | Alt+L, or the Config sidebar "Topology wires" checkbox |
| **Grouped + tinted collage** | `static/js/collage.js` | F3 / `desktop_collage` MCP / command palette |
| **Live appearance** | `desktop.js` `handleSessionCreated` + server `notify_portal_session_created` | A session is created via `agentwire new` / `worktree` / the portal |

All four share one palette (`--lineage-tint-1..6`) and one state vocabulary (`--topology-awaiting`, `--topology-stuck`) — see [Design tokens](#design-tokens) below. Static reference for those tokens (with the enforcement rules) also lives in the `agentwire-desktop-ui` skill's "Topology Design Tokens" section.

## Born-from-parent placement

When a session's parent window is open and a child of it is created, the child's window doesn't just pop into existence — it "flies" out of the parent's title bar to its landing spot, then mounts as a real window. Two modules split the job:

- **`desktop.js`** tracks *who was just born* (`recentBirths`, a one-shot, 15-second-TTL ticket per child id — `BIRTH_TTL_MS`) and computes the two rects: `parentTitleBarRect()` (a slice of the parent's `.wb-header`, clamped to 80–220px wide) as the start, and the desktop area as the end.
- **`spawn-ghost.js`**'s `flyGhost(fromRect, toRect, tintVar, onSettle)` animates a plain overlay `<div>` (`.spawn-ghost`, tinted via `--ghost-tint`) between those two rects over 480ms (`FLY_MS`), then calls `onSettle`.

**The real WinBox window is only constructed in `onSettle`** — never transformed mid-flight. This mirrors the discipline `window-collage.md`'s autopsy documents for the collage (#235): animating or resizing a *real* WinBox window mid-transition corrupts its internal geometry/min-stack bookkeeping and fires its `ResizeObserver` into a PTY resize storm. The ghost is a disposable DOM node with no WinBox state to protect, so it's the only thing that ever moves.

**Graceful fallback, in two independent layers:**
- `flyGhost()` itself skips the animation and calls `onSettle()` synchronously whenever there's no usable `fromRect` (parent not open, or minimized — `parentTitleBarRect()` returns `null` for both) or the browser reports `prefers-reduced-motion: reduce`. Placement still happens — just instantly, no ghost shown.
- If the parent isn't open at all, `registerBirth()` never calls `openSessionTerminal()` in the first place — the child is just recorded in `desktop.sessions` and shows up in the sidebar list like any other session, with no auto-open and no ghost. "Watch it get born" only fires while you're already looking at the parent.

Birth detection has two independent paths that land in the same place (`registerBirth()`): the poll-driven `sessions` event diff (`handleSessionsListUpdate`, comparing against a baseline snapshot so page-load's existing world is never treated as a batch of births) and the live `session_created` push (see [Live appearance](#live-appearance) below) as a pure accelerant on top of it — session creation still gets a birth ghost even if the live event never arrives.

## Connector overlay

**Alt+L** toggles a read-only SVG overlay (`.topology-wires-overlay`, `topology-wires.js`) drawing a bezier "wire" from each open parent window's title bar down to each of its open children's title bars. It's also toggleable from the Config sidebar's "Topology wires" checkbox (same `topologyWires.setVisible()` call); the on/off state persists across reloads in `localStorage['aw-topology-wires-visible']` (visible by default).

Verified in `desktop.js`'s `setupTopologyWires()`: bound on `keydown` in the capture phase, gated on `e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyL'`, with the command palette / help modal open as an escape hatch and `e.repeat` ignored — the same idiom as the F3 collage toggle and the Alt+]/Alt+[ window-cycling bindings.

**Strictly read-only**, the same discipline as the collage: it only calls `getBoundingClientRect()` on each window's `.wb-header` to anchor a wire's endpoints, and never writes to a window's geometry. A minimized window (`display: none` via this app's `.winbox.min` override) collapses its rect to all-zero, which the module treats as "no anchor" — the wire for that pair is simply dropped until the window is restored, rather than hanging off the desktop's top-left corner.

**Wire state** mirrors each child's activity, reusing the same `activityStates` map the sidebar's status dot reads:
- **idle** — the default, no state class, plain lineage-tinted line at low opacity.
- **flow** — `processing`/`generating`/`playing`/`active` — a dashed line animates along its length (`topology-wire-flow`).
- **awaiting** — the child's `state === 'needs_input'` — overrides the lineage tint with `--topology-awaiting` (amber) and pulses.
- **stuck** — `state === 'off'` — overrides with `--topology-stuck` (red) and pulses faster.

Failure-state parity is enforced in the CSS cascade order, not `!important`: the `--awaiting`/`--stuck` classes are declared after the base `.topology-wire` rule specifically so an equal-specificity override wins and a blocked/awaiting child's wire can never read as "fine" just because its family tint happens to look calm.

**Wire color** is a stable per-family hash (`tintIndexForRoot`, hashing the family-root session name into one of the 6 `--lineage-tint-N` slots) — the same 6-slot palette the collage and the birth-ghost use, computed independently rather than imported from `lineage.js` (see the open follow-up below).

**Z-band:** the overlay sits at z-index 900 — above WinBox windows (whose inline z-indexes grow from 10) but below the collage overlay (1400), so entering the collage always wins and the wires never bleed through it.

The redraw loop is a self-stopping `requestAnimationFrame` tick: it only keeps scheduling itself while at least one wire is actually on-screen (`_hasPairs`), and is woken by session/window/activity events (`onSessionsChanged`, `window_registered/unregistered/minimized/restored/tiled`, `viewport_resize`, `session_activity`, TTS/audio events) rather than polling on a timer.

## Grouped + tinted collage

F3 (or the `desktop_collage` MCP tool, or the command palette) still enters the same Mission Control-style preview overlay documented in [Window collage](window-collage.md) — but the grid cells are now **families** (a session + its descendants), not raw windows (#748). `collage.js#_groupFamilies()` walks each window's `.parent` chain (via `_lineageOf()`, sessions-section.js's tree-linkage data) to a root, and groups every open window under that root. A singleton family (no open children) renders as a plain tile with a faint tint hint (`.collage-family.is-singleton`); a family with open children renders as a tinted cluster (`.collage-family`) — the parent's tile on top (`.collage-family-parent`), its children nested in a wrapping row below (`.collage-family-children`, reserved ~42% of the cluster height, scrolling vertically rather than ever overflowing the grid horizontally).

Family hue cycles through `--lineage-tint-1..6` by **family index within the current grid** (`familyIndex % 6`), set inline as `--family-tint` — a different hashing scheme than the connector overlay's per-root hash (see the open follow-up below). The grid's cols×rows fitting and the underlying preview-tile mechanics (live monitor WebSocket per session tile, cloned iframe per artifact tile, the "never touch a real WinBox window" invariant) are unchanged — see `window-collage.md` for that architecture and its autopsy.

## Live appearance

A newly created session used to only appear once the next `sessions_update` poll landed. `agentwire new` (and `agentwire worktree`, which delegates into the same `cmd_new` code path) now posts an extra event as early as possible during session creation — before the potentially slow first-message wait — so the desktop can react immediately (#747):

1. **CLI side** (`session_cli.py cmd_new` → `agentwire/core.py notify_portal_session_created(session_name, created_by, kind)`): fire-and-forget POST to the portal's `/api/notify` with `{"event": "session_created", "session": ..., "parent": ..., "role": ...}` (`parent`/`role` omitted from the payload entirely when not set, rather than sent as null).
2. **Server side** (`agentwire/routes/notify.py api_notify`, the `session_created` branch): looks up the session's fresh record as a fallback for `parent`/`role` if the payload didn't carry them (covers the plain global tmux `session-created` hook, which only ever knows the bare session name), then broadcasts `session_created` with `{session, name, parent, role}` to every connected dashboard, immediately followed by a full `sessions_update`.
3. **Client side** (`desktop.js handleSessionCreated`): merges a placeholder record into `desktop.sessions` right away (deduped by name; the `sessions_update` that follows moments later always wins with the authoritative record) and re-emits `sessions`, which is what actually drives `registerBirth()` for the born-from-parent placement above. This is a pure accelerant on top of the poll-driven diff path — a session still gets placed correctly if this event is dropped or arrives late, just with poll lag.

Note: `handleSessionCreated`'s destructuring includes a `machine` field, read defensively for a future creation path — as of this writing neither `notify_portal_session_created` nor the server's `session_created` broadcast actually populates `machine` in the payload, so it's always `undefined` today.

## Design tokens

Both design rules that gate every rule appended to `desktop.css`'s `/* === topology === */` anchor:

1. **Lineage tint SSOT** — `--lineage-tint-1` through `--lineage-tint-6` (green/blue/purple/pink/orange/cyan) are the one palette every topology surface derives fills/borders/glows from via `color-mix()`, rather than hardcoding a family hex anywhere else. Red and amber are reserved for state (below) and must never be assigned as a lineage tint.
2. **Failure-state parity** — `--topology-awaiting` (amber, aliases `--orb-awaiting`) and `--topology-stuck` (red, aliases `--neon-red`) are the shared state vocabulary; every "alive" treatment a surface ships (glow, pulse, flowing wire) must have an equally salient blocked/awaiting counterpart, so a stuck or awaiting-input child never reads as "fine" next to an active sibling.

**Live-pane-peek safety flag:** any live-pane content peek (e.g. a collage tile rendering a child's actual terminal output) must default off or blurred — surfacing a child session's live terminal by default is a screen-share / credential-exposure risk the moment topology becomes something people demo (council flag, #749). Peeks are opt-in reveals, never the resting state. Today's collage tiles already stream live content by design (see `window-collage.md`) — this rule governs any *future* peek surface layered on top of the topology visualization, not a retrofit of the existing collage.

Both rules, plus `prefers-reduced-motion` handling for the wire animations, live in the CSS comment block at `desktop.css`'s `/* === topology === */` anchor.

## Open follow-up

**#755** — unify lineage-tint slot assignment across the three surfaces. #749 made the tint *palette* SSOT but not the *assignment*, so all three still pick a family's slot differently: placement calls `lineage.js`'s `lineageTintVar()` (string-hash of the family root), the connector overlay has its own near-identical hash in `tintIndexForRoot()`, and the collage instead keys on the family's *position in the current grid* (`familyIndex % 6`, which isn't even stable across rebuilds). Nothing is broken by this today (each surface's own tint is internally consistent), but the same family can render a different hue in the collage than on its connector wire or its birth ghost. The proposed fix points `topology-wires.js` and `collage.js` at `lineage.js` instead of each re-deriving its own assignment.
