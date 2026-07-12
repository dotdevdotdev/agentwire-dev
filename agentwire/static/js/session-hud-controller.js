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
 */

import { desktop } from './desktop-manager.js';
import { TopologyView } from './topology-render.js';
import { getAllSessions, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { subtreeOf } from './lineage.js';
import { mountCardTerminal, mountSelfMic } from './card-terminal.js';
import { sessionHud } from './session-hud.js';

class HudController {
    constructor() {
        this._view = null;
        this._resolveWindowSession = null;
        /** @type {string|null} focused session name, or null = global tree */
        this._contextSession = null;
        /** @type {string|null} window id backing _contextSession, for window_unregistered matching */
        this._contextWindowId = null;
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
        });

        // Seed from whatever's already focused when the HUD first mounts,
        // rather than waiting for the next focus change.
        this._applyFocus(desktop.getActiveWindow());

        ensureSessionsLoaded();
        this._render();
        onSessionsChanged(() => this._render());

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
        const session = this._resolveWindowSession(id);
        if (!session || session === this._contextSession) return;
        this._contextSession = session;
        this._contextWindowId = id;
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
        this._view.setSelfSession(this._contextSession);
        this._view.render(this._contextSession ? subtreeOf(this._contextSession, sessions) : sessions);
    }

    _expandCard(name, session, slotEl) {
        sessionHud.growToHalf();
        const cleanup = mountCardTerminal(name, session, slotEl, { topologyView: this._view });
        return () => {
            cleanup();
            sessionHud.restoreDetent();
        };
    }
}

export const hudController = new HudController();
