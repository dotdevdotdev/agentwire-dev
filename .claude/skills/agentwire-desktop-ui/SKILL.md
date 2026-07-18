---
name: agentwire-desktop-ui
description: Portal desktop UI patterns — left sidebar (click-toggle tab handle, accordion sections, session grouping into Sessions/Services via explicit allowlist, keyboard nav), session window modes (Monitor `<pre>` vs Terminal xterm.js — Monitor MUST NOT use xterm), artifact windows (sandboxed iframes from `~/.agentwire/artifacts/`), window collage (preview overlay — NEVER mutate real WinBox windows), topology design tokens (lineage-tint hue families + failure-state parity for placement/connector/collage). Use when editing portal static files (`static/js/sidebar/*`, `desktop.js`, `desktop.css`), changing window behavior, adding sidebar sections, or building session-topology UI (parent/child visualization).
---

# Portal Desktop UI Patterns

## Left Sidebar (click-toggle tab handle)

The portal uses a left sidebar with a floating tab handle instead of hover hotzone. A small tab (›) peeks from the left edge — click to slide sidebar open, click again to close. Click outside or press Escape to dismiss. Pin to keep visible (reflows desktop area).

**Structure:**
- **Tab handle**: floating 20×40px button on left edge, rides sidebar when open, chevron flips direction
- **Header**: connection status dot, session count, clock, pin toggle
- **Open Windows section**: lists currently-open windows (drag to reorder, click to focus, × to close). Persisted in `localStorage['taskbar-state']` — restores on refresh.
- **Accordion sections**: Sessions, Services, Machines, Projects, Artifacts, Scheduler, Safety, Config. Click header to expand/collapse. Data fetched on first expand.
- **Footer**: global PTT button, voice indicator

**Session grouping:** Sessions are split into two accordion sections based on the name:
- **Services**: infrastructure sessions. The built-in allowlist in `service-classification.js` (`agentwire-portal`, `agentwire-tts`, `agentwire-stt`, `agentwire-scheduler`, `agentwire-notifications`) is merged at load time with config-defined custom services fetched from `/api/services/custom` (driven by `services.custom` in `config.yaml`). When the fetch resolves, `notifyListeners()` re-renders so flagged sessions hop into this column.
- **Sessions**: everything else (working sessions, worktrees, remote sessions)

Both share session data from `sessions-section.js` (single fetch, shared activity state, pub-sub via `onSessionsChanged`).

**Keyboard:** Alt+] cycles forward through open windows, Alt+[ cycles backward — works everywhere including inside terminals (captured on `window` in capture phase before xterm, detected via `e.code` bracket keys). Tab and Shift+Tab are NEVER intercepted by the desktop; they always pass through to the focused terminal (Claude Code uses Tab for completion and Shift+Tab for permission modes — #659/#696).

**Files:** `static/js/sidebar.js` (shell + click-toggle), `static/js/sidebar/<name>-section.js` (per-section modules), `static/css/desktop.css` (sidebar-* classes).

## Command Palette & Idea-First Capture

`Cmd/Ctrl+K` opens the command palette (`static/js/command-palette.js`) **straight into the `new-idea` view** — Esc reaches the root menu (New idea / New session / New worktree / Open session / Window collage). The full-width 💡 **New idea** sidebar button (between header and sections) and the Projects section `+` route to the same view.

**New-idea flow (issue #253):** idea textarea (Enter submits, Shift+Enter newline, 🎤 dictation via `voice/browser-stt.js`) → kebab-case project name derived client-side (`deriveProjectName`, stopword-stripped; stops auto-updating once the user edits it) → submit POSTs `/api/projects/create` then `/api/create` with `first_message` → window opens immediately while the idea is delivered to the booting agent in the background (server calls `agentwire send --wait-ready`; failure posts a toast). There is no name-first modal anymore — `new-project-modal.js` was deleted.

## Session Window Modes

| Mode | Element | Use Case |
|------|---------|----------|
| **Monitor** | `<pre>` with ANSI-to-HTML | Read-only output viewing, polls `tmux capture-pane` |
| **Terminal** | xterm.js | Interactive terminal, attaches via `tmux attach` |

**Important:** Monitor mode must use a simple `<pre>` element, NOT xterm.js. xterm.js requires precise container dimensions for its fit addon to work correctly. Since monitor mode just displays captured text output, a `<pre>` element with `white-space: pre-wrap` and ANSI-to-HTML conversion is simpler and more reliable.

**Per-session PTT** lives in the WinBox titlebar (next to the activity indicator), not as a floating button.

## Window Collage (Mission Control)

F3 or Alt/Option+` / `desktop_collage` MCP / command palette → grid of live previews of every open window; click a tile to focus, Esc to exit.

**Grid cells are families, not windows (#748):** `collage.js#_groupFamilies()` walks each window's `.parent` chain to a root and clusters every open descendant under it — a singleton family (no open children) is a plain tile, a family with children renders as a tinted cluster (parent tile on top, children in a wrapping row below). Hue cycles `--lineage-tint-1..6` by the family's position in the current grid.

**The one rule: tiles are overlay-local previews — NEVER mutate the real WinBox windows.** Session tiles stream pane content over a second monitor WS (`/ws/{sessionId}`, rendered via shared `utils/ansi.js`); artifact tiles are cloned iframes. Real windows are never moved/resized/transformed/un-minimized, so exit has nothing to restore.

Why this is load-bearing (each broke a previous implementation — full autopsy in `docs/wiki/internals/window-collage.md`):
- Faking `winbox.min` corrupts WinBox's internal min-stack → later minimizes re-lay windows out as 250×35px bars, with duplicate entries compounding per cycle.
- WinBox animates geometry over 300ms → `getBoundingClientRect` right after a write returns mid-transition garbage.
- Resizing a terminal window fires ResizeObserver → `fitAddon.fit()` + PTY resize per frame → resizes the real tmux session and corrupts the xterm WebGL layer (transparent windows).
- `registerWindow`/`setActiveWindow` auto-minimize all others (single-window mode) → any window event mid-overlay fights manual layouts.

**Z-index landscape:** WinBox windows (inline, grows from 10) < collage overlay (1400) < toasts (1500) < modals (2000) < command palette (3000) < sidebar / Session HUD drawer (9001) < tile drag overlay (99999).

## Session Topology (#745–#749, #761–#764)

Mechanisms that make the parent→child session tree visible and alive on the desktop (full reference: `docs/wiki/internals/session-topology.md`):

- **Born-from-parent placement** (`spawn-ghost.js`, #745) — when you open a just-born session's window while its parent's window is open, the child's window flies out of the parent's title bar (a disposable overlay ghost, `flyGhost()`) before the real WinBox window is constructed in `onSettle` — never transform a real window mid-open (same #235 discipline as the collage). Falls back to instant placement with no ghost when the parent isn't open/minimized or `prefers-reduced-motion` is set. **A spawn no longer auto-opens that window** — the Session HUD owns spawn awareness now (see below); this animates a manual open. `FLY_MS` and `prefersReducedMotion()` are exported for reuse by the HUD spawn animation (`session-hud-spawn.js`).
- **Shared topology renderer** (`topology-render.js`'s `TopologyView`, #761) — mount-agnostic session-family card renderer, the one engine behind the two surfaces below. Narrow-first flex-wrap layout (never an absolutely-positioned wide canvas), idempotent `render()` (diffs cards/rows/links in place). `mode: 'window'` = solid chrome (workspace window), `mode: 'shade'` = compact full-width left-anchored cards (Session HUD).
- **Session Workspace window** (`workspace-window.js`, #762) — hosts `TopologyView` (`mode: 'window'`) as a first-class WinBox window per family (keyed by family root), opened via the 🛰 launcher on any session card.
- **Session HUD** (`session-hud.js` + `session-hud-controller.js` + `session-hud-spawn.js`, epic #775–781) — a pull-down top-edge frosted shade, the third `TopologyView` mount (`mode: 'shade'`). Context-following (re-roots on `active_window_changed` — focused session becomes a dimmed you-are-here root, children interactive), a Sessions∣Services segmented control (reuses `services-section.js`), card→mini-terminal (shared `card-terminal.js`), and ghost cards for session-less worktrees (Clean up / Adopt via thin `worktree` CLI wrappers). On `session_created` it auto-peeks and flies a parent→child ghost (`aw-hud-autopeek-on-spawn` pref), then retracts — this **absorbed and deleted** the old phantom overlay (`topology-overlay.js`, #764) and **superseded** the born-from-parent window auto-open (#745), so a spawn no longer opens/maximizes a window. Drawer at z 9001; mutually exclusive with the sidebar + scratchpad. Two detents (peek 33vh / half 50vh), Alt+P or the top pull handle, flush-left via `--hud-left`.
- **Grouped + tinted collage** (`collage.js`, #748) — see "Window Collage" above.
- **Live appearance** (`desktop.js` `handleSessionCreated` + `agentwire/core.py` `notify_portal_session_created`, #747) — `agentwire new`/`worktree` posts a `session_created` event as soon as the session exists, so the desktop merges it into the live list (and can trigger the Session HUD spawn peek + parent→child fly animation) without waiting for the next `sessions_update` poll.

## Topology Design Tokens (#749)

Session-family visualization (born-from-parent placement, shared renderer + its two mounts, hierarchy-grouped collage) is a design gate on top of two rules, enforced via shared CSS tokens in `desktop.css` `:root` — the `/* === topology === */` anchor near the end of the file is where those slices append their rules.

1. **Lineage tint** — a family (a parent + all its descendants) shares one hue, never re-derived per surface. `--lineage-tint-1` through `--lineage-tint-6` are the SSOT palette (assign by `family-index % 6`); derive fills/borders/glows from the base var via `color-mix()` rather than hardcoding a family hex anywhere else. Red and amber are reserved for state (below) — never assign them as a lineage tint.
2. **Failure-state parity** — every "alive" treatment (glow, pulse, flowing wire) ships an equally salient blocked/crashed/awaiting-input counterpart. `--topology-awaiting` (amber, aliases `--orb-awaiting`) and `--topology-stuck` (red, aliases `--neon-red`, covers blocked + crashed alike) are the shared vocabulary — a green pulse must never be the only signal while a child is stuck on a permission prompt.

**Also:** any live-pane content peek (e.g. a collage tile showing a child's actual terminal output) must default off/blurred — surfacing it by default is a screen-share / credential-exposure risk the moment topology becomes something people demo. Peeks are opt-in reveals, never the resting state.

## Artifact Windows

Agents can display HTML content in sandboxed iframe windows on the portal desktop.

**Agent workflow (MCP):**
```python
# Write HTML and announce in one step (click-to-open notification, #817 —
# artifacts never force-open a window or steal focus)
desktop_write_artifact(filename="dashboard.html", html_content="<h1>Hello</h1>", title="Dashboard")

# Or announce an existing file or external URL
desktop_open_artifact(url="dashboard.html", title="Dashboard")
desktop_open_artifact(url="https://example.com", title="External")
```

**Files served from:** `~/.agentwire/artifacts/` via `/artifacts/` route.

**Sandboxing:** Local files get `allow-scripts allow-same-origin`. External URLs get `allow-scripts allow-forms allow-popups` (no same-origin).
