# Window Collage (Mission Control)

> Living wiki. Update this page, don't create new versions.

F3 or Alt/Option+` (or the `desktop_collage` MCP tool, or the command palette → "Window collage") lays a live preview of every open window into a grid so the whole desktop can be scanned at once. Click a tile to focus that window; Esc, the hotkey again, or clicking the backdrop exits.

Module: `agentwire/static/js/collage.js`. Styles: the `.collage-*` block in `agentwire/static/css/desktop.css`. Hotkey wiring: `setupCollage()` in `agentwire/static/js/desktop.js`.

## The one rule

**The tiles are previews — the real windows are never touched.**

Entering and exiting the collage is pure DOM addition/removal in an overlay layer. The real WinBox windows are never moved, resized, transformed, minimized, restored, or refocused mid-overlay. Exit has no restore step because nothing changed. This is the entire design, and it is load-bearing: every prior implementation that manipulated the real windows failed in ways that could not be patched (see the autopsy below).

Any future feature that wants to show multiple windows at once — exposé variants, window pickers, drag previews, "peek" modes — must follow the same rule: build overlay-local live views, never re-layout the real windows.

## How it works

| Piece | Implementation |
|-------|----------------|
| **Backdrop** | `.collage-overlay`, a fully opaque (`#0a0c10`) layer over the desktop area. Opaque is deliberate: any translucency lets the maximized window behind ghost through the gaps (bright artifact pages especially). No `backdrop-filter` — a blur here leaves a stale compositor layer that renders windows translucent after exit. |
| **Grid** | cols = `round(sqrt(n × aspect))` fitted to the desktop area aspect, CSS Grid with `1fr` tracks. Rebuilt (not patched) on any churn. |
| **Session tiles** | A second *monitor* WebSocket per tile (`/ws/{sessionId}`) — the server pushes the current pane content on connect and re-broadcasts on change (500ms poll). Rendered as ANSI→HTML via the shared `utils/ansi.js` into a `<pre>` sized at full desktop dimensions, then `transform: scale()`d into the tile (overlay-local element, so the transform is harmless). Bottom-anchored so the visible part is the live screen. Audio messages on tile sockets play through `desktop._playAudio` (device-level dedupe prevents double-play with a real window attached). |
| **Artifact tiles** | A cloned `<iframe>` with the live window's `src` and sandbox attributes, scaled the same way, `pointer-events: none`. |
| **Other windows** | Title-only fallback card ("no preview"). |
| **Name labels** | Big centered pill (`.collage-card-label`) over each tile — session name / artifact title, sized via `--collage-label-size` (tracks cell height, 15–30px). Fades to ~12% opacity on hover so the live preview underneath stays inspectable. Plain rgba pill, no backdrop-filter. |
| **Click-to-focus** | Tile click → tear down overlay → `desktop.setActiveWindow(id)` — the same battle-tested path as a sidebar tab click. Nothing collage-specific touches window state. |
| **Mid-overlay churn** | `window_registered` / `window_unregistered` → rebuild the grid. `active_window_changed` (Tab cycle, sidebar click, a freshly-opened window's focus) → tear down and get out of the way. `viewport_resize` → rebuild. |
| **Keyboard** | Entering blurs the focused element so keystrokes can't leak into the xterm `<textarea>` behind the backdrop; exit refocuses the active window. Esc is capture-phase and defers to the command palette when it's open. |
| **Alt/Option+` dead key** | On macOS, Option+` starts a grave-accent composition against the focused xterm textarea *before* keydown fires, and the composed `` ` `` arrives via `composition*` events that `preventDefault()` cannot cancel — the reason this hotkey leaked backticks historically. `setupCollage()` swallows composition events at the **window capture phase** for ~700ms after the hotkey (xterm's composition listeners are target-phase on the textarea, so nothing reaches the PTY) and clears the textarea's value on the suppressed `compositionend` (the browser commits the char natively; residue would skew xterm's composition position bookkeeping). The suppressor is disarmed if the toggle was a no-op (<2 windows), so the dead key composes normally when the collage isn't in play. |

Tile sockets are closed on every teardown/rebuild — verified by watching the server's `Client connected (total: N)` logs stay flat across many cycles.

### Z-index landscape

The overlay must sit above windows but below everything that should remain usable on top of it:

| Layer | z-index |
|-------|---------|
| WinBox windows | inline, grows from 10 (+1 per focus) |
| **Collage overlay** | **1400** |
| Notification toasts | 1500 |
| Modals | 2000 |
| Command palette | 3000 |
| Sidebar / sidebar tab | 9001 / 9002 |
| Tile drag overlay | 99999 |

(The original scrim used 5000, which silently put the command palette *behind* the collage.)

## Autopsy: why mutating the real windows can never work

The first implementations (resize-into-grid, then transform-scale-the-live-windows) burned days in whack-a-mole — every fix surfaced new edge cases. That wasn't bad luck; the approach required perfectly saving and restoring state across four systems with independent timing. The specific failure modes, so nobody re-derives them:

### 1. WinBox's internal min-stack (the "thin bars" bug)

WinBox keeps minimized windows in a private array. Every real `minimize()` triggers a stack re-layout that **resizes every stack entry to a ~250×35px bar** (the built-in minimize-bar UI; this app hides them via `.winbox.min { display: none }`). "Un-minimizing" a window by removing the `.min` class and faking `winbox.min = false`:

- leaves the window in the stack, so any later minimize anywhere re-lays it out as a visible thin bar;
- makes the next `minimize()` push a **duplicate** stack entry (WinBox only guards on the faked flag), so corruption compounds every enter/exit cycle — the "every fix spawns 4 new issues" feeling.

Only `winbox.minimize()` / `restore()` / `maximize()` maintain that stack. Never fake the flags or classes.

### 2. Geometry reads race WinBox's 300ms transition

Stock WinBox CSS animates `width/height/left/top` over 300ms on every window. Setting a window's geometry and then calling `getBoundingClientRect()` returns the **old (mid-transition) box**, not the target — so any layout math based on read-after-write geometry is garbage. (This is also why FLIP animations disable transitions during measurement.) If you must read window geometry, read it from a window that hasn't been written to in >300ms, or add WinBox's `no-animation` class first.

### 3. The ResizeObserver → fit() → PTY resize storm

Each terminal window has a ResizeObserver wired to `fitAddon.fit()` + a `resize` message over the terminal WebSocket. Growing a hidden window to full size (or shrinking one into a grid cell) fires that observer **on every frame of the 300ms animation**, which:

- spams `fit()` at transient sizes, reflowing the xterm buffer repeatedly;
- **resizes the real PTY/tmux session** server-side — tmux clamps to the smallest attached client, so "peeking" at the desktop was resizing actual sessions for every attached client;
- corrupts the xterm WebGL compositing layer — the window's background renders transparent after restore until a manual repaint (`_forceRepaint()` in `session-window.js` exists for the legitimate resize paths: tile ↔ maximize).

### 4. Single-window-mode invariants fight any overlay layout

`registerWindow()` and `setActiveWindow()` enforce one-maximized-window mode by minimizing all others. Any window event mid-overlay (a session opening, an MCP focus call, Tab cycling) re-minimized every "tile" (`display: none` → windows vanishing) while the overlay's relayout fought to undo it — two owners for the same state.

### Why tiling never had these problems

Drag-to-tile is a **one-way, static end-state** mutation: set geometry once, emit `window_tiled`, refit. No hidden→shown transitions, no restore-to-exact-prior-state obligation, no mid-flight measurement. The collage by contrast was a round-trip through every piece of hidden state — until the round-trip was deleted.

## Scale

There is no hard window-count limit — the grid formula handles any `n ≥ 2` (verified live with 15 windows / a 4×4 grid: all tiles streaming, click-to-focus instant, zero console errors, and killing 12 sessions mid-overlay live-collapsed the grid 15→3 cleanly). The practical bounds:

- **Server poll cost** — each session tile holds a monitor WS, which keeps that session's `_poll_output` task running (`tmux capture-pane` every 500ms, thread-pool executor). Local sessions are a few ms each; **remote sessions poll over SSH** (hundreds of ms each), so a collage of many remote sessions is the first thing that would feel heavy. Cost exists only while the overlay is open — tile sockets close on exit.
- **Legibility** — tiles shrink ~1/√n; beyond ~12 windows they're activity thumbnails identified by their title bars, same as macOS Mission Control.
- **No tmux impact at any count** — monitor WSes are capture-pane based, not client attaches: no PTY, no session resize, regardless of tile count.

## Known unrelated noise

xterm 5.3 occasionally throws a spontaneous `Cannot read properties of undefined (reading 'dimensions')` from page-load window materialization / disposed terminals. It fires with the collage closed and predates it — don't attribute it to the overlay.
