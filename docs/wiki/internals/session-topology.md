# Session Topology (parent → child visualization)

> Living wiki. Update this page, don't create new versions.

Mechanisms, shipped across #745–#749, #761–#764, and the Session HUD epic #775–#781, that make the parent→child session tree visible and alive on the desktop instead of only existing in the sidebar's nested list:

| Mechanism | Module | Trigger |
|-----------|--------|---------|
| **Born-from-parent placement** | `static/js/spawn-ghost.js` (+ wiring in `desktop.js`) | Manually opening a just-born child's window (within the birth-ticket TTL) while its parent's window is open |
| **Shared topology renderer** | `static/js/topology-render.js` (`TopologyView`) | Mounted by the two surfaces below — not triggered directly |
| **Session Workspace window** | `static/js/workspace-window.js` | 🛰 launcher on a session card, or `openSessionWorkspace()` |
| **Card mini-terminal** | `static/js/terminal-pane.js` (`TerminalPane`) + `workspace-window.js` | Click a card in the Workspace window |
| **Session HUD** (pull-down shade) | `static/js/session-hud.js` + `session-hud-controller.js` + `session-hud-spawn.js` | Alt+P / the top-edge pull handle; auto-peeks on the live `session_created` event |
| **Grouped + tinted collage** | `static/js/collage.js` | F3 / `desktop_collage` MCP / command palette |
| **Live appearance** | `desktop.js` `handleSessionCreated` + server `notify_portal_session_created` | A session is created via `agentwire new` / `worktree` / the portal |

All share one palette (`--lineage-tint-1..6`) and one state vocabulary (`--topology-awaiting`, `--topology-stuck`) — see [Design tokens](#design-tokens) below. Static reference for those tokens (with the enforcement rules) also lives in the `agentwire-desktop-ui` skill's "Topology Design Tokens" section.

## Born-from-parent placement

When you open a just-born session's window (within the 15-second birth-ticket TTL, `BIRTH_TTL_MS`) while its parent's window is open, the child's window doesn't just pop into existence — it "flies" out of the parent's title bar to its landing spot, then mounts as a real window. (A spawn itself no longer *auto*-opens that window — the [Session HUD](#session-hud-pull-down-topology-shade) below owns spawn awareness now; this fly animates a *manual* open.) Two modules split the job:

- **`desktop.js`** tracks *who was just born* (`recentBirths`, a one-shot, 15-second-TTL ticket per child id — `BIRTH_TTL_MS`) and computes the two rects: `parentTitleBarRect()` (a slice of the parent's `.wb-header`, clamped to 80–220px wide) as the start, and the desktop area as the end.
- **`spawn-ghost.js`**'s `flyGhost(fromRect, toRect, tintVar, onSettle)` animates a plain overlay `<div>` (`.spawn-ghost`, tinted via `--ghost-tint`) between those two rects over 480ms (`FLY_MS`), then calls `onSettle`.

**The real WinBox window is only constructed in `onSettle`** — never transformed mid-flight. This mirrors the discipline `window-collage.md`'s autopsy documents for the collage (#235): animating or resizing a *real* WinBox window mid-transition corrupts its internal geometry/min-stack bookkeeping and fires its `ResizeObserver` into a PTY resize storm. The ghost is a disposable DOM node with no WinBox state to protect, so it's the only thing that ever moves.

**Graceful fallback, in two independent layers:**
- `flyGhost()` itself skips the animation and calls `onSettle()` synchronously whenever there's no usable `fromRect` (parent not open, or minimized — `parentTitleBarRect()` returns `null` for both) or the browser reports `prefers-reduced-motion: reduce`. Placement still happens — just instantly, no ghost shown.
- `registerBirth()` no longer opens the child's window on spawn at all — #745's auto-open (a child Monitor window flying out and maximizing) was removed once the HUD took over spawn awareness, because it hijacked the screen on every worker spawn. `registerBirth()` just records the child in `desktop.sessions` (so it shows up in the sidebar + HUD) plus a short-lived `recentBirths` ticket. The ghost only flies if you then *manually* open that session's window while the parent is open; otherwise there's no ghost. "Watch it get born" is now the HUD's job.

Birth detection has two independent paths that land in the same place (`registerBirth()`): the poll-driven `sessions` event diff (`handleSessionsListUpdate`, comparing against a baseline snapshot so page-load's existing world is never treated as a batch of births) and the live `session_created` push (see [Live appearance](#live-appearance) below) as a pure accelerant on top of it. The live path additionally drives the HUD spawn peek (`triggerSpawnPeek`, see [Session HUD](#session-hud-pull-down-topology-shade) below); the poll path just records the session — it still appears in the sidebar + HUD, only without the peek animation.

## Shared topology renderer + Session Workspace window

`topology-render.js`'s `TopologyView` (#761) is the one mount-agnostic engine behind both surfaces below — "one engine, two mounts." Given a container and a session list, it groups sessions into families (`lineage.js`'s `groupFamilies`) and renders one card per session (status dot, name, role chip, activity sparkline, machine tag) plus curved SVG links from each card to its parent's, all tinted by the family's `lineageTintVar`. `render()` is idempotent — repeat calls diff cards/rows/links against the previous pass rather than tearing down and rebuilding, so a spawn or kill mid-view doesn't flash the tree. Cards lay out in normal flex-wrap flow, never an absolutely-positioned wide canvas, because the owner runs the portal in a narrow ~1/3-width window.

`wireStateFor(name, record)` is the one shared status mapping ('idle' | 'flow' | 'awaiting' | 'stuck') every card reads, so a card and the sidebar dot never disagree on what "awaiting"/"stuck" means.

**`workspace-window.js`'s `WorkspaceWindow`** (#762) hosts `TopologyView` in `mode: 'window'` (solid chrome) as a first-class WinBox window — the 🛰 launcher on any session card in a family opens (or focuses) the one window for that family, keyed by family root so it doesn't matter which member you launched it from. Opens with `desktop.minimizeAllExcept(null)` maximized, re-renders on every `onSessionsChanged` tick, and disposes its `TopologyView` on close.

## Card mini-terminal

`TopologyView` (#763) supports an optional `onCardExpand(name, session, slotEl)` callback: clicking a card toggles an inline expand — the card grows to full row width, TopologyView appends an empty `.topology-card-expand-slot` into it, and calls `onCardExpand`, which mounts whatever content belongs there and returns a cleanup function. TopologyView owns the DOM lifecycle end to end: it calls that cleanup on re-click (collapse), when the underlying session disappears from a `render()` pass (pruned mid-expand), or when the whole view is disposed — the mounting code never has to track that itself. Only one card is expanded at a time (accordion), so at most one extra live WS connection is open. Clicks inside the slot are guarded (`e.target.closest('.topology-card-expand-slot')`) so interacting with the mounted content never bubbles into a collapse toggle.

`workspace-window.js` is the only consumer today: its `_mountCardTerminal(name, session, slotEl)` mounts a `TerminalPane` (`terminal-pane.js` — the xterm + WS core extracted out of `SessionWindow`'s full terminal window, `new TerminalPane(container, {session, machine})` with `focus()`/`fit()`/`dispose()`) plus a titlebar-style mic button wired the same way `SessionWindow._setupPTT`'s is (`PttController` → `/transcribe` → auto-send or an edit-before-send `.wb-transcript-bar`). A small "⤢" button pops out to the full `SessionWindow` via a dynamic `import('./desktop.js')` (avoids a circular import — `desktop.js` is what constructs `WorkspaceWindow`). `SessionWindow` itself now just wraps `TerminalPane` with WinBox chrome, the titlebar PTT button, and the activity indicator — Monitor mode (a polling `<pre>` dump, not xterm) is untouched and keeps its own WS/reconnect machinery in `session-window.js`, since it's a different beast entirely.

**Satisfies the live-pane-peek safety flag** ([Design tokens](#design-tokens) below): the mini-terminal only mounts on an explicit card click — opening the Workspace window itself shows no live terminal content, so the resting state stays inert and the peek is always an opt-in reveal, never automatic.

## Session HUD (pull-down topology shade)

The **Session HUD** (epic #775) is a pull-down top-edge frosted-glass shade — the **third mount surface** for `TopologyView`, alongside the Session Workspace window above and (until #780) the now-deleted phantom overlay. It's the always-available, glanceable situational-awareness layer: pull it down (Alt+P or the top-center handle) to see the live topology, click a card to drop into its mini-terminal, and it's where the spawn-relationship animation now plays. `session-hud.js` owns the drawer chrome (mirroring `scratchpad.js`'s edge-drawer mechanic), `session-hud-controller.js` drives the content, `session-hud-spawn.js` the spawn choreography.

**Shell (#776):** a frosted drawer (`backdrop-filter: blur(20px)`) that drops from the top edge, flush to the left (`--hud-left`), spanning full width. Two detents — **peek** (33vh) ↔ **half** (50vh) — via a top-center pull handle (drag to snap) or Alt+P. Mutually exclusive with the left sidebar and right scratchpad (opening one closes the others), the same coordination those drawers already share.

**Shade layout (#777):** `TopologyView` gains `mode: 'shade'` — a compact, full-width, left-anchored variant (denser 128px cards, families flowing left-to-right) for the short, narrow surface, instead of the workspace window's centered solid chrome. The canvas scrolls horizontally; a dot-grid texture (the `--dot-grid-image`/`--dot-grid-size` tokens shared with the Workspace window, applied as the image **only** — no opaque fill — so the frost shows through) reads behind the cards.

**Context-following (#778):** the shade's default view follows window focus. Nothing focused → all root families (global tree). A **session window focused** → the shade re-roots onto that session's family: the focused session becomes a dimmed, non-interactive **"you-are-here" root** (header-only PTT mic, no drill-in — you're already in it), its children the interactive cards. Re-roots **live** on `desktop`'s `active_window_changed`, so Alt+]/Alt+[ window-cycling updates the shade in real time; focus on a non-session window retains the last session context. Clicking an interactive card mounts a mini-terminal into it (shared `card-terminal.js`, extracted from the Workspace window's card-terminal mount — one implementation, two mounts) and auto-grows the shade to half.

**Sessions ∣ Services (#779):** a segmented control in the header swaps the topology for the sidebar's live Services list — the `servicesSection` singleton (`sidebar/services-section.js`) mounted into a sibling container, one fetch/render/start-stop source, no duplication. The topology canvas is only hidden (never unmounted) on switch, so focus re-rooting survives a round-trip; the choice persists to `localStorage['aw-hud-segment']`.

**Absorbs the spawn animation (#780):** the standalone phantom overlay (`topology-overlay.js`, #764) is **deleted** — its spawn-relationship glimpse now lives in the HUD. On a live `session_created` with a known parent, `session-hud-spawn.js`'s `triggerSpawnPeek()` auto-peeks the shade (if closed, gated on the `aw-hud-autopeek-on-spawn` pref, on by default), flies a `spawn-ghost.js` ghost from the parent's card to the new child's (both already rendered by the controller's re-render), then retracts after a ~2600ms linger — unless the user grabs the pull handle meanwhile, which cancels the retract. The birth-ghost fly timing (`FLY_MS`) and `prefersReducedMotion()` are still shared from `spawn-ghost.js`. **This supersedes the born-from-parent window auto-open** (#745): a spawn no longer opens or maximizes a child Monitor window over your screen — the HUD is the spawn-awareness surface, and worker windows open on demand (see [Born-from-parent placement](#born-from-parent-placement) above).

**Ghost cards for session-less worktrees (#781):** worktree folders with no live session (the `orphan` state from `agentwire worktree --list --all`, plus a disk-scan fallback) render as dimmed, dashed **ghost cards** — a "no session" badge, branch + worktree path — placed in their family when the dead session's recorded `created_by` still resolves, else in an "unattached" cluster. Two confirm-gated actions per ghost: **Clean up** (`POST /api/worktree/cleanup` → `agentwire worktree --remove`, the plain form — honors the merge/open-PR safety guard, surfaces the refusal reason, never escalates to `--force-delete-branch`) and **Adopt** (`POST /api/worktree/adopt` → `agentwire worktree <name> --existing --created-by <parent>`, re-parenting the new session onto the family so it reports back). Both endpoints are thin CLI wrappers — no git/registry logic in the portal.

**Narrow-first polish:** the shade cards show the **session name, not the role chip** — at the fixed 128px width the non-shrinking "ORCHESTRATOR" chip starved the name, so the chip is hidden in shade mode (role still shows on the expanded card + the Workspace window). The handle centers on the shade via `--hud-left`, and `_pruneCards` removes a pruned card's SVG `<path>` so re-rooting never leaves a dangling connector.

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
