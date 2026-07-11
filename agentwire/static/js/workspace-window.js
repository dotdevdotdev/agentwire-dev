/**
 * workspace-window.js
 *
 * WorkspaceWindow — the "home base" surface for a session family (#762):
 * a first-class WinBox window that hosts the shared topology renderer
 * (topology-render.js's TopologyView, #761) on a canvas. One window per
 * root family — identity is the family's root session name, not the
 * particular session the launcher was clicked from, so the 🛰 button
 * always lands on the same window regardless of which member opened it.
 * Mirrors ReviewWindow's WinBox lifecycle (register/unregister via
 * desktop-manager.js, `_createWinBox` + guarded close).
 */

import { desktop } from './desktop-manager.js';
import { TopologyView } from './topology-render.js';
import { getAllSessions, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { familyRootName } from './lineage.js';

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

export class WorkspaceWindow {
    /**
     * @param {Object} options
     * @param {string} options.rootSession - Family root session name (the window's identity)
     * @param {string} options.windowId - Unique window identifier
     * @param {HTMLElement} options.root - Parent element for WinBox
     * @param {(name: string, session: object) => void} [options.onCardClick] - Fired on card click
     * @param {Function} options.onClose - Callback when window closes
     * @param {Function} options.onFocus - Callback when window gains focus
     */
    constructor(options) {
        this.rootSession = options.rootSession;
        this.windowId = options.windowId;
        this.root = options.root || document.body;
        this.title = `🛰 ${this.rootSession}`;
        this.onCardClickCallback = options.onCardClick || null;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;

        this.winbox = null;
        this.isOpen = false;
        this._topologyView = null;
        this._unsubscribe = null;
    }

    open() {
        if (this.isOpen) { this.focus(); return; }
        const container = this._createContainer();
        this._createWinBox(container);
        this.isOpen = true;

        const canvas = container.querySelector('.workspace-window-canvas');
        this._topologyView = new TopologyView(canvas, {
            onCardClick: (name, session) => this.onCardClickCallback?.(name, session),
            mode: 'window',
        });

        ensureSessionsLoaded();
        this._render();
        this._unsubscribe = onSessionsChanged(() => this._render());
    }

    close() {
        if (!this.isOpen) return;
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        if (this._topologyView) {
            this._topologyView.dispose();
            this._topologyView = null;
        }
        if (this.winbox) {
            const wb = this.winbox;
            this.winbox = null;
            wb.close();
        }
        desktop.unregisterWindow(this.windowId);
        this.isOpen = false;
        if (this.onCloseCallback) this.onCloseCallback(this);
    }

    focus() { if (this.winbox) this.winbox.focus(); }
    minimize() { if (this.winbox) this.winbox.minimize(); }
    restore() { if (this.winbox) this.winbox.restore(); }
    get isMinimized() { return this.winbox ? this.winbox.min : false; }

    _createContainer() {
        const container = document.createElement('div');
        container.className = 'workspace-window-content';
        container.innerHTML = `
            <div class="workspace-toolbar">
                <span class="workspace-toolbar-icon">🛰</span>
                <span class="workspace-toolbar-title" title="${esc(this.rootSession)}">${esc(this.rootSession)}</span>
            </div>
            <div class="workspace-window-canvas desktop-dot-grid"></div>
        `;
        return container;
    }

    _createWinBox(container) {
        this.winbox = new WinBox({
            title: this.title,
            icon: '<span style="font-size:14px">&#x1F6F0;</span>',
            mount: container,
            root: this.root,
            width: '80%',
            height: '80%',
            x: 'center',
            y: 'center',
            minwidth: 320,
            minheight: 320,
            class: ['workspace-window'],
            onclose: () => { this.winbox = null; this.close(); return false; },
            onfocus: () => { if (this.onFocusCallback) this.onFocusCallback(this); },
            onminimize: () => desktop.emit('window_minimized', { id: this.windowId }),
            onrestore: () => {
                desktop.emit('window_restored', { id: this.windowId });
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
        });
        desktop.registerWindow(this.windowId, this.winbox);
    }

    /** Re-render the family against the latest session list. Called on open
     * and whenever onSessionsChanged fires (poll update or the session_created
     * accelerant, both funneled through the same sessions-section.js feed). */
    _render() {
        if (!this._topologyView) return;
        const sessions = getAllSessions();
        const members = sessions.filter((s) => {
            const name = s.name || '';
            return name && familyRootName(name, sessions) === this.rootSession;
        });
        this._topologyView.render(members);
    }
}
