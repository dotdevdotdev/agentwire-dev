/**
 * session-hud-controller.js
 *
 * The context-following brain behind the Session HUD shade (#778): decides
 * WHAT the drawer renders, and wires up card-click → mini-terminal the same
 * way the Session Workspace window does.
 *
 * Mounts a `TopologyView(mode:'shade')` (#777) into the HUD's canvas and
 * subscribes to two feeds:
 *   - the sessions feed (`onSessionsChanged`, sidebar/sessions-section.js)
 *   - window-focus changes (`desktop`'s `active_window_changed`, fired by
 *     every window kind's onFocus → `desktop.setActiveWindow`)
 *
 * Context-following view selection:
 *   - No session window focused → render ALL root families (the global
 *     tree) — just `render(sessions)`, letting TopologyView's own
 *     `groupFamilies` call do the grouping.
 *   - A session window focused → re-root onto that session's family
 *     (`subtreeOf`, lineage.js) and present it as if its card were clicked:
 *     the focused session becomes a dimmed, non-interactive "you-are-here"
 *     root (`TopologyView.setSelfSession`); its descendants are the
 *     interactive cards.
 * Focusing an artifact/review/workspace/council window resolves to no
 * session (`resolveWindowSession` returns null for non-`kind:'session'`
 * windows) and is therefore a no-op — the last session context is retained,
 * per spec. `resolveWindowSession` is injected via `init()` rather than
 * imported from desktop.js directly: desktop.js is the module that boots
 * this controller, and desktop.js↔session-hud-controller.js would otherwise
 * be a static circular import (the same reason `card-terminal.js`'s
 * "open full terminal" button dynamic-imports desktop.js instead).
 *
 * Ghost cards (#781): worktree folders left on disk with no live session
 * (`agentwire worktree --list`'s "orphan" state) are polled from
 * `/api/worktrees` separately from the live sessions feed — they're not
 * sessions, so they don't belong in sidebar/sessions-section.js's shared
 * `getAllSessions()` — and merged into the array passed to `render()` as
 * pseudo-session records (`state: 'orphan'`, plus `branch`/`worktreePath`/
 * `projectPath`). Each ghost's `parent` is whatever `--list` resolved as its
 * dead session's recorded creator (may be undefined): `groupFamilies`/
 * `lineageOf` (lineage.js) already treat an unresolvable parent name as "this
 * is its own root", so a ghost with no known lineage naturally lands as its
 * own single-card family — the "unattached / needs cleanup" case — with zero
 * extra grouping logic here.
 */

import { desktop } from './desktop-manager.js';
import { TopologyView } from './topology-render.js';
import { getAllSessions, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { subtreeOf } from './lineage.js';
import { mountCardTerminal, mountSelfMic } from './card-terminal.js';
import { sessionHud } from './session-hud.js';
import { apiFetch } from './api.js';
import { normalizeMachine } from './session-id.js';
import { toastSuccess, toastError } from './toast.js';

const GHOST_POLL_MS = 20000;

class HudController {
    constructor() {
        this._view = null;
        this._resolveWindowSession = null;
        /** @type {string|null} focused session name, or null = global tree */
        this._contextSession = null;
        /** @type {boolean} "master" mode (showAll) — pin the global tree and
         * ignore focus re-rooting until the drawer is closed. */
        this._global = false;
        /** @type {string|null} window id backing _contextSession, for window_unregistered matching */
        this._contextWindowId = null;
        /** @type {Array<object>} pseudo-session records for orphaned worktrees, refreshed via polling */
        this._ghosts = [];
        this._ghostTimer = null;
    }

    /**
     * @param {HTMLElement} canvas - `.session-hud-canvas`, sessionHud's mount point
     * @param {(id: string|null) => string|null} resolveWindowSession - desktop.js's
     *   getWindowSession — resolves a focused window id to a session name, or null
     *   for a non-session window (or nothing focused)
     */
    init(canvas, resolveWindowSession) {
        this._resolveWindowSession = resolveWindowSession;

        this._view = new TopologyView(canvas, {
            mode: 'shade',
            onCardExpand: (name, session, slotEl) => this._expandCard(name, session, slotEl),
            onSelfMount: (name, session, cardEl) => mountSelfMic(name, session, cardEl),
            onGhostCleanup: (name, session) => this._cleanupGhost(name, session),
            onGhostAdopt: (name, session) => this._adoptGhost(name, session),
            onCardOpen: (name, session) => this._openSession(name, session),
            onCardKill: (name, session) => this._killSession(name, session),
        });

        // Seed from whatever's already focused when the HUD first mounts,
        // rather than waiting for the next focus change.
        this._applyFocus(desktop.getActiveWindow());

        ensureSessionsLoaded();
        this._render();
        onSessionsChanged(() => this._render());

        this._fetchGhosts();
        this._ghostTimer = setInterval(() => this._fetchGhosts(), GHOST_POLL_MS);

        // Closing the drawer exits "master" mode, so the next Alt+P open is the
        // normal context-following view again.
        sessionHud.onClose(() => { this._global = false; });

        desktop.on('active_window_changed', ({ id }) => this._applyFocus(id));
        // Closing the focused session's own window (with nothing else taking
        // focus) falls back to the global tree, rather than leaving the HUD
        // re-rooted onto a session that's no longer open anywhere.
        desktop.on('window_unregistered', ({ id }) => {
            if (id && id === this._contextWindowId) {
                this._contextSession = null;
                this._contextWindowId = null;
                this._render();
            }
        });
    }

    _applyFocus(id) {
        if (this._global) return; // master mode pins the global tree
        const session = this._resolveWindowSession(id);
        if (!session || session === this._contextSession) return;
        this._contextSession = session;
        this._contextWindowId = id;
        this._render();
    }

    /**
     * "Master" view — open the HUD showing the full session tree (every family,
     * not the focused session's subtree) and pin it there: focus changes no
     * longer re-root until the drawer closes. Entry points are the Sessions
     * sidebar-header icon and the "Show all sessions" command-palette action
     * (no hotkey — the plain Alt+P peek is the context-following view). Idempotent.
     */
    showAll() {
        this._global = true;
        this._contextSession = null;
        this._contextWindowId = null;
        if (sessionHud.segment !== 'sessions') sessionHud.setSegment('sessions');
        if (!sessionHud.open) sessionHud.toggle(true);
        this._render();
    }

    _render() {
        if (!this._view) return;
        const sessions = getAllSessions();
        // The context session may have closed/renamed since it was last
        // focused — fall back to the global tree rather than rendering an
        // empty subtree for a name nothing matches anymore. Gated on
        // sessions.length: an empty list during the page-boot window (the
        // sessions fetch hasn't resolved yet, e.g. mid-restoreTaskbarState())
        // means "no data yet", not "this session is gone" — resetting on
        // that would wipe a just-restored focus before it ever got to render.
        if (this._contextSession && sessions.length > 0 && !sessions.some((s) => s.name === this._contextSession)) {
            this._contextSession = null;
            this._contextWindowId = null;
        }
        const merged = this._ghosts.length ? [...sessions, ...this._ghosts] : sessions;
        this._view.setSelfSession(this._contextSession);
        this._view.render(this._contextSession ? subtreeOf(this._contextSession, merged) : merged);
    }

    _expandCard(name, session, slotEl) {
        sessionHud.growToHalf();
        const cleanup = mountCardTerminal(name, session, slotEl, { topologyView: this._view });
        return () => {
            cleanup();
            sessionHud.restoreDetent();
        };
    }

    // ⋯ menu "Open window" — pop the session into its own full terminal window.
    // Dynamic-imports desktop.js for the same reason card-terminal.js does: it's
    // the module that boots this controller, so a static import would be circular.
    async _openSession(name, session) {
        try {
            const { openSessionTerminal } = await import('./desktop.js');
            openSessionTerminal(name, 'terminal', normalizeMachine(session?.machine));
        } catch (e) {
            toastError(`Couldn't open ${name}: ${e.message}`);
        }
    }

    // ⋯ menu "Kill session" — the two-step confirm already happened in-menu
    // (topology-render.js), so this fires straight through. DELETE /api/sessions
    // is the same thin `agentwire kill` wrapper the sidebar close button uses; the
    // resulting lifecycle push re-renders the tree without this card.
    async _killSession(name, session) {
        try {
            const res = await apiFetch(`/api/sessions/${encodeURIComponent(name)}`, { method: 'DELETE' });
            if (!res.ok) {
                const reason = await res.text().catch(() => '') || `HTTP ${res.status}`;
                toastError(`Kill failed for ${name}: ${reason}`);
                return;
            }
            toastSuccess(`Killed ${name}`);
        } catch (e) {
            toastError(`Kill failed for ${name}: ${e.message}`);
        }
    }

    // Ghosts (#781) aren't sessions — no WS push tells us when a worktree dir
    // disappears or a dead one reappears — so this is a plain poll, refreshed
    // eagerly right after an action instead of waiting out the interval.
    async _fetchGhosts() {
        try {
            const res = await apiFetch('/api/worktrees');
            const data = await res.json();
            this._ghosts = (data.entries || [])
                .filter((e) => e.exists && !e.alive)
                .map((e) => this._toGhostRecord(e));
        } catch (e) {
            this._ghosts = [];
        }
        this._render();
    }

    _toGhostRecord(e) {
        const branch = e.branch || (e.worktree_path || '').split('/').filter(Boolean).pop() || e.session;
        return {
            name: e.session,
            parent: e.created_by || undefined,
            state: 'orphan',
            branch,
            worktreePath: e.worktree_path,
            projectPath: e.project,
        };
    }

    async _cleanupGhost(name, session) {
        try {
            const res = await apiFetch('/api/worktree/cleanup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: session.branch, project: session.projectPath }),
            });
            const result = await res.json().catch(() => ({}));
            if (!res.ok || result.success === false) {
                const reason = result.error || `HTTP ${res.status}`;
                toastError(`Clean up failed for ${name}: ${reason}`);
                return { error: reason };
            }
            toastSuccess(`Removed worktree ${session.branch || name}`);
            const note = (result.branch && !result.branch_deleted && result.branch_note)
                ? `Removed — branch kept: ${result.branch_note}`
                : null;
            await this._fetchGhosts();
            return note ? { note } : {};
        } catch (e) {
            toastError(`Clean up failed for ${name}: ${e.message}`);
            return { error: e.message };
        }
    }

    async _adoptGhost(name, session) {
        try {
            const res = await apiFetch('/api/worktree/adopt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: session.branch,
                    project: session.projectPath,
                    createdBy: session.parent || undefined,
                }),
            });
            const result = await res.json().catch(() => ({}));
            if (!res.ok || result.success === false) {
                const reason = result.error || `HTTP ${res.status}`;
                toastError(`Adopt failed for ${name}: ${reason}`);
                return { error: reason };
            }
            toastSuccess(`Adopted ${result.session || name}`);
            await this._fetchGhosts();
            return {};
        } catch (e) {
            toastError(`Adopt failed for ${name}: ${e.message}`);
            return { error: e.message };
        }
    }
}

export const hudController = new HudController();
