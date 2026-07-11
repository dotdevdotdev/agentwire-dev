# Session Topology (parent → child visualization)

> Living wiki. Update this page, don't create new versions.

Mechanisms, shipped across #745–#749 and #761–#764, that make the parent→child session tree visible and alive on the desktop instead of only existing in the sidebar's nested list:

| Mechanism | Module | Trigger |
|-----------|--------|---------|
| **Born-from-parent placement** | `static/js/spawn-ghost.js` (+ wiring in `desktop.js`) | A child session appears while its parent's window is open |
| **Shared topology renderer** | `static/js/topology-render.js` (`TopologyView`) | Mounted by the two surfaces below — not triggered directly |
| **Session Workspace window** | `static/js/workspace-window.js` | 🛰 launcher on a session card, or `openSessionWorkspace()` |
| **Phantom overlay** | `static/js/topology-overlay.js` | The live `session_created` event (a child spawns), or the Config sidebar "Topology overlay" checkbox |
| **Grouped + tinted collage** | `static/js/collage.js` | F3 / `desktop_collage` MCP / command palette |
| **Live appearance** | `desktop.js` `handleSessionCreated` + server `notify_portal_session_created` | A session is created via `agentwire new` / `worktree` / the portal |

All share one palette (`--lineage-tint-1..6`) and one state vocabulary (`--topology-awaiting`, `--topology-stuck`) — see [Design tokens](#design-tokens) below. Static reference for those tokens (with the enforcement rules) also lives in the `agentwire-desktop-ui` skill's "Topology Design Tokens" section.

## Born-from-parent placement

When a session's parent window is open and a child of it is created, the child's window doesn't just pop into existence — it "flies" out of the parent's title bar to its landing spot, then mounts as a real window. Two modules split the job:

- **`desktop.js`** tracks *who was just born* (`recentBirths`, a one-shot, 15-second-TTL ticket per child id — `BIRTH_TTL_MS`) and computes the two rects: `parentTitleBarRect()` (a slice of the parent's `.wb-header`, clamped to 80–220px wide) as the start, and the desktop area as the end.
- **`spawn-ghost.js`**'s `flyGhost(fromRect, toRect, tintVar, onSettle)` animates a plain overlay `<div>` (`.spawn-ghost`, tinted via `--ghost-tint`) between those two rects over 480ms (`FLY_MS`), then calls `onSettle`.

**The real WinBox window is only constructed in `onSettle`** — never transformed mid-flight. This mirrors the discipline `window-collage.md`'s autopsy documents for the collage (#235): animating or resizing a *real* WinBox window mid-transition corrupts its internal geometry/min-stack bookkeeping and fires its `ResizeObserver` into a PTY resize storm. The ghost is a disposable DOM node with no WinBox state to protect, so it's the only thing that ever moves.

**Graceful fallback, in two independent layers:**
- `flyGhost()` itself skips the animation and calls `onSettle()` synchronously whenever there's no usable `fromRect` (parent not open, or minimized — `parentTitleBarRect()` returns `null` for both) or the browser reports `prefers-reduced-motion: reduce`. Placement still happens — just instantly, no ghost shown.
- If the parent isn't open at all, `registerBirth()` never calls `openSessionTerminal()` in the first place — the child is just recorded in `desktop.sessions` and shows up in the sidebar list like any other session, with no auto-open and no ghost. "Watch it get born" only fires while you're already looking at the parent.

Birth detection has two independent paths that land in the same place (`registerBirth()`): the poll-driven `sessions` event diff (`handleSessionsListUpdate`, comparing against a baseline snapshot so page-load's existing world is never treated as a batch of births) and the live `session_created` push (see [Live appearance](#live-appearance) below) as a pure accelerant on top of it — session creation still gets a birth ghost even if the live event never arrives.

## Shared topology renderer + Session Workspace window

`topology-render.js`'s `TopologyView` (#761) is the one mount-agnostic engine behind both surfaces below — "one engine, two mounts." Given a container and a session list, it groups sessions into families (`lineage.js`'s `groupFamilies`) and renders one card per session (status dot, name, role chip, activity sparkline, machine tag) plus curved SVG links from each card to its parent's, all tinted by the family's `lineageTintVar`. `render()` is idempotent — repeat calls diff cards/rows/links against the previous pass rather than tearing down and rebuilding, so a spawn or kill mid-view doesn't flash the tree. Cards lay out in normal flex-wrap flow, never an absolutely-positioned wide canvas, because the owner runs the portal in a narrow ~1/3-width window.

`wireStateFor(name, record)` is the one shared status mapping ('idle' | 'flow' | 'awaiting' | 'stuck') every card (and the phantom overlay below) reads, so a card and the sidebar dot never disagree on what "awaiting"/"stuck" means.

**`workspace-window.js`'s `WorkspaceWindow`** (#762) hosts `TopologyView` in `mode: 'window'` (solid chrome) as a first-class WinBox window — the 🛰 launcher on any session card in a family opens (or focuses) the one window for that family, keyed by family root so it doesn't matter which member you launched it from. Opens with `desktop.minimizeAllExcept(null)` maximized, re-renders on every `onSessionsChanged` tick, and disposes its `TopologyView` on close.

## Phantom overlay

`topology-overlay.js` (#764) mounts `TopologyView` in `mode: 'overlay'` (translucent glass cards) as a transient, non-interactive pop-over: the instant a live `session_created` event names a child with a known parent, `desktop.js`'s `handleSessionCreated` calls `topologyOverlay.pop(sessionName, allSessions)`, which resolves the child's family (root + descendants, `lineage.js`'s `familyRootName`) and renders it into a fixed-position panel that fades in, lingers ~2.6s (`LINGER_MS`), then fades out and tears itself down. This replaces the old connector-overlay's "see the topology over your terminals" job (#746, deleted by #764 — it only read sensibly on a wide tiled desktop the owner never runs) and needs no open parent window to fire.

**Non-interactive by default:** `pointer-events: none` on `.topology-overlay-root` is a CSS-inherited property, so it cascades through every card `TopologyView` renders — the overlay never blocks a click through to the terminal underneath. Only the small `×` dismiss button opts back into `pointer-events: auto`; clicking it hides the current pop immediately (skipping any remaining linger) without touching the settings toggle, so the *next* spawn still pops normally.

**Animation timing is shared with the birth ghost**, not reinvented: `topology-overlay.js` imports `FLY_MS` (480ms) and `prefersReducedMotion()` from `spawn-ghost.js` (both now exported for reuse) rather than picking its own duration or re-querying `matchMedia`. `prefers-reduced-motion: reduce` skips the pop-in transition entirely (`.topology-overlay-root--instant`) — the overlay just appears — matching `flyGhost()`'s own reduced-motion fallback.

**Settings toggle:** the Config sidebar's "Topology overlay" checkbox calls `topologyOverlay.setVisible()`, persisted to `localStorage['aw-topology-overlay-visible']` (visible by default) — same pattern the old connector overlay used for its own `VISIBLE_KEY`. Turning it off also dismisses whatever pop is currently on screen.

**Z-band:** the overlay sits at z-index 1000 — above WinBox windows (whose inline z-indexes grow from 10) but below the collage overlay (1400) and toasts/modals (1500/2000), so a spawn's ambient glimpse never competes with something the user actually needs to act on.

A pop while a previous one is still showing/fading reuses the same panel and DOM (`TopologyView.render()`'s idempotent diffing) and restarts the linger, rather than stacking a second overlay on top.

## Grouped + tinted collage

F3 (or the `desktop_collage` MCP tool, or the command palette) still enters the same Mission Control-style preview overlay documented in [Window collage](window-collage.md) — but the grid cells are now **families** (a session + its descendants), not raw windows (#748). `collage.js#_groupFamilies()` walks each window's `.parent` chain (via `_lineageOf()`, sessions-section.js's tree-linkage data) to a root, and groups every open window under that root. A singleton family (no open children) renders as a plain tile with a faint tint hint (`.collage-family.is-singleton`); a family with open children renders as a tinted cluster (`.collage-family`) — the parent's tile on top (`.collage-family-parent`), its children nested in a wrapping row below (`.collage-family-children`, reserved ~42% of the cluster height, scrolling vertically rather than ever overflowing the grid horizontally).

Family hue comes from `lineage.js`'s `lineageTintVar()`, set inline as `--family-tint` — the same root-hash every topology surface uses (#755, unified). The grid's cols×rows fitting and the underlying preview-tile mechanics (live monitor WebSocket per session tile, cloned iframe per artifact tile, the "never touch a real WinBox window" invariant) are unchanged — see `window-collage.md` for that architecture and its autopsy.

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

Both rules, plus `prefers-reduced-motion` handling for the topology animations, live in the CSS comment block at `desktop.css`'s `/* === topology === */` anchor.
