---
name: agentwire-desktop-ui
description: Portal desktop UI patterns — left sidebar (click-toggle tab handle, accordion sections, session grouping into Sessions/Services via explicit allowlist, keyboard nav), session window modes (Monitor `<pre>` vs Terminal xterm.js — Monitor MUST NOT use xterm), artifact windows (sandboxed iframes from `~/.agentwire/artifacts/`), window collage (preview overlay — NEVER mutate real WinBox windows). Use when editing portal static files (`static/js/sidebar/*`, `desktop.js`, `desktop.css`), changing window behavior, or adding sidebar sections.
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

**Keyboard:** Tab cycles forward through open windows, Shift+Tab cycles backward. Works inside terminals (captured on `window` in capture phase before xterm).

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

**The one rule: tiles are overlay-local previews — NEVER mutate the real WinBox windows.** Session tiles stream pane content over a second monitor WS (`/ws/{sessionId}`, rendered via shared `utils/ansi.js`); artifact tiles are cloned iframes. Real windows are never moved/resized/transformed/un-minimized, so exit has nothing to restore.

Why this is load-bearing (each broke a previous implementation — full autopsy in `docs/wiki/internals/window-collage.md`):
- Faking `winbox.min` corrupts WinBox's internal min-stack → later minimizes re-lay windows out as 250×35px bars, with duplicate entries compounding per cycle.
- WinBox animates geometry over 300ms → `getBoundingClientRect` right after a write returns mid-transition garbage.
- Resizing a terminal window fires ResizeObserver → `fitAddon.fit()` + PTY resize per frame → resizes the real tmux session and corrupts the xterm WebGL layer (transparent windows).
- `registerWindow`/`setActiveWindow` auto-minimize all others (single-window mode) → any window event mid-overlay fights manual layouts.

**Z-index landscape:** WinBox windows (inline, grows from 10) < collage overlay (1400) < toasts (1500) < modals (2000) < command palette (3000) < sidebar (9001) < tile drag overlay (99999).

## Artifact Windows

Agents can display HTML content in sandboxed iframe windows on the portal desktop.

**Agent workflow (MCP):**
```python
# Write HTML and open in one step
desktop_write_artifact(filename="dashboard.html", html_content="<h1>Hello</h1>", title="Dashboard")

# Or open an existing file or external URL
desktop_open_artifact(url="dashboard.html", title="Dashboard")
desktop_open_artifact(url="https://example.com", title="External")
```

**Files served from:** `~/.agentwire/artifacts/` via `/artifacts/` route.

**Sandboxing:** Local files get `allow-scripts allow-same-origin`. External URLs get `allow-scripts allow-forms allow-popups` (no same-origin).
