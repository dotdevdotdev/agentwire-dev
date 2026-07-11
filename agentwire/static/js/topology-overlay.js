/**
 * topology-overlay.js
 *
 * Phantom topology overlay (#764) — the ambient "watch it happen" glimpse of
 * the architecture: the instant a child session spawns, the shared renderer
 * (topology-render.js's TopologyView, #761) pops up over the live terminal
 * windows showing the new session's family (root + descendants), lingers
 * long enough to register, then fades. Same engine the Session Workspace
 * window (#762) hosts as a first-class window — "one engine, two mounts" —
 * mounted here with `mode: 'overlay'` for translucent glass cards instead of
 * the workspace's solid chrome.
 *
 * Non-interactive by design: `pointer-events: none` on the root cascades
 * through every card TopologyView renders (pointer-events is CSS-inherited,
 * same trick spawn-ghost-root and the old connector overlay used) so the
 * overlay never blocks a click through to the terminal underneath — only the
 * dismiss control opts back into `pointer-events: auto`. This module never
 * touches a real WinBox window's geometry, same discipline as spawn-ghost.js
 * and collage.js: it's a disposable DOM layer, not a window.
 *
 * Supersedes topology-wires.js (#746, deleted by #764) as the "see the
 * topology over your terminals" mechanism — the wire overlay only read
 * sensibly on a wide tiled desktop the owner never runs; this pops as a
 * narrow-first panel (matching topology-render.js's narrow-first layout)
 * regardless of window arrangement, and needs no open parent window to fire.
 *
 * @module topology-overlay
 */

import { TopologyView } from './topology-render.js';
import { familyRootName } from './lineage.js';
import { prefersReducedMotion, FLY_MS } from './spawn-ghost.js';

const VISIBLE_KEY = 'aw-topology-overlay-visible';
export const TOPOLOGY_OVERLAY_EVENT = 'topology-overlay-change';

/** Above WinBox windows (inline z-indexes grow from 10), below the collage
 * overlay (1400) and toasts/modals (1500/2000) — a spawn's ambient glimpse
 * should never compete with something the user actually needs to act on. */
const OVERLAY_Z = 1000;

/** How long the overlay stays fully visible before it starts fading — long
 * enough to register as "a session was just born", short enough to not sit
 * over the terminal you're trying to read. */
const LINGER_MS = 2600;

class TopologyOverlay {
    constructor() {
        this._visible = localStorage.getItem(VISIBLE_KEY) !== '0';
        /** @type {HTMLElement|null} */
        this._root = null;
        /** @type {HTMLElement|null} */
        this._panel = null;
        /** @type {TopologyView|null} */
        this._view = null;
        this._lingerTimer = null;
        this._hideTimer = null;
    }

    get visible() {
        return this._visible;
    }

    setVisible(v) {
        this._visible = v;
        if (v) localStorage.removeItem(VISIBLE_KEY);
        else localStorage.setItem(VISIBLE_KEY, '0');
        window.dispatchEvent(new CustomEvent(TOPOLOGY_OVERLAY_EVENT));
        if (!v) this._dismiss();
    }

    toggle() {
        this.setVisible(!this._visible);
    }

    /**
     * Pop the overlay for `sessionName`'s family (root + descendants) — call
     * the instant a session_created event names a child with a known
     * parent. No-op when the settings toggle is off. A pop while a previous
     * one is still showing/fading reuses the same panel and restarts the
     * linger, rather than stacking a second overlay.
     *
     * @param {string} sessionName
     * @param {Array<object>} allSessions
     */
    pop(sessionName, allSessions) {
        if (!this._visible || !sessionName) return;
        const root = familyRootName(sessionName, allSessions);
        const members = (allSessions || []).filter(
            (s) => s.name && familyRootName(s.name, allSessions) === root
        );
        if (!members.length) return;

        this._clearTimers();
        this._ensureDom();
        this._view.render(members);
        this._show();
    }

    _ensureDom() {
        if (this._root) return;

        this._root = document.createElement('div');
        this._root.className = 'topology-overlay-root';
        this._root.style.zIndex = String(OVERLAY_Z);
        this._root.style.setProperty('--topology-overlay-fade-ms', `${FLY_MS}ms`);

        this._panel = document.createElement('div');
        this._panel.className = 'topology-overlay-panel';
        this._root.appendChild(this._panel);

        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'topology-overlay-dismiss';
        dismiss.setAttribute('aria-label', 'Dismiss');
        dismiss.textContent = '×';
        dismiss.addEventListener('click', () => this._dismiss());
        this._panel.appendChild(dismiss);

        document.body.appendChild(this._root);

        this._view = new TopologyView(this._panel, { mode: 'overlay' });
    }

    _show() {
        this._root.classList.toggle('topology-overlay-root--instant', prefersReducedMotion());
        // Force layout, then defer the class add to the next frame — same
        // two-step spawn-ghost.js uses. A single synchronous reflow isn't
        // enough on its own: without the rAF, the browser never paints the
        // opacity:0 starting state (it's created and flipped to visible in
        // the same tick), so there's nothing to transition FROM and the
        // fade-in silently no-ops.
        void this._root.getBoundingClientRect();
        requestAnimationFrame(() => {
            this._root?.classList.add('topology-overlay-root--visible');
        });

        this._lingerTimer = setTimeout(() => this._fade(), LINGER_MS);
    }

    _fade() {
        this._lingerTimer = null;
        if (!this._root) return;
        this._root.classList.remove('topology-overlay-root--visible');
        this._hideTimer = setTimeout(() => this._reset(), prefersReducedMotion() ? 0 : FLY_MS);
    }

    /** Per-session dismiss: hide now, skipping any remaining linger. The
     * settings toggle staying on means the *next* spawn still pops normally
     * — this only silences the one currently on screen. */
    _dismiss() {
        this._clearTimers();
        this._fade();
    }

    _reset() {
        this._hideTimer = null;
        if (this._view) {
            this._view.dispose();
            this._view = null;
        }
        if (this._root) {
            this._root.remove();
            this._root = null;
            this._panel = null;
        }
    }

    _clearTimers() {
        if (this._lingerTimer) {
            clearTimeout(this._lingerTimer);
            this._lingerTimer = null;
        }
        if (this._hideTimer) {
            clearTimeout(this._hideTimer);
            this._hideTimer = null;
        }
    }
}

export const topologyOverlay = new TopologyOverlay();
