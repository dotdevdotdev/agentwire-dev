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
 *
 * Card interaction (#763): clicking a card expands it inline into a mini
 * terminal — a TerminalPane (terminal-pane.js, the same xterm+WS core
 * SessionWindow's full terminal window uses) plus a PttController mic,
 * wired the same way SessionWindow's titlebar PTT is. TopologyView owns the
 * expand/collapse DOM lifecycle (including cleanup when a session vanishes
 * mid-expand); this module only supplies what to mount.
 */

import { desktop } from './desktop-manager.js';
import { TopologyView } from './topology-render.js';
import { getAllSessions, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { familyRootName } from './lineage.js';
import { TerminalPane } from './terminal-pane.js';
import { PttController } from './ptt.js';
import { apiFetch } from './api.js';
import { buildSessionId, normalizeMachine } from './session-id.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';

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
     * @param {Function} options.onClose - Callback when window closes
     * @param {Function} options.onFocus - Callback when window gains focus
     */
    constructor(options) {
        this.rootSession = options.rootSession;
        this.windowId = options.windowId;
        this.root = options.root || document.body;
        this.title = `🛰 ${this.rootSession}`;
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
            onCardExpand: (name, session, slotEl) => this._mountCardTerminal(name, session, slotEl),
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

    /**
     * Mount a mini-terminal (#763) into a card's expand slot: a TerminalPane
     * over the session's real WS, plus a titlebar-style mic button wired the
     * same way SessionWindow's PTT is (record → /transcribe → auto-send or
     * edit-before-send bar). Returns a dispose function TopologyView calls on
     * collapse/prune/view-teardown.
     *
     * @param {string} name - Session name
     * @param {object} session - Session record (for machine)
     * @param {HTMLElement} slotEl - Empty slot appended into the card
     * @returns {() => void} cleanup
     */
    _mountCardTerminal(name, session, slotEl) {
        const machine = normalizeMachine(session?.machine);
        const sessionId = buildSessionId(name, machine);

        // Actions (mic + open-full) live in the card's own header row next to
        // the role chip — not a dedicated toolbar row — so the mini-terminal
        // reclaims that vertical space. topology-render.js's card-click collapse
        // toggle exempts .topology-card-actions the same way it exempts the slot.
        const card = slotEl.closest('.topology-card');
        const headerRow = card?.querySelector('.topology-card-top');
        const actions = document.createElement('div');
        actions.className = 'topology-card-actions';

        const pttBtn = document.createElement('button');
        pttBtn.type = 'button';
        pttBtn.className = 'wb-title-ptt';
        pttBtn.title = 'Hold to record voice input';
        pttBtn.innerHTML = '<span class="ptt-icon">🎤</span>';

        const openBtn = document.createElement('button');
        openBtn.type = 'button';
        openBtn.className = 'topology-card-mini-open';
        openBtn.title = 'Open full terminal';
        openBtn.textContent = '⤢';
        openBtn.addEventListener('click', async () => {
            const { openSessionTerminal } = await import('./desktop.js');
            openSessionTerminal(name, 'terminal', machine);
        });

        actions.append(pttBtn, openBtn);
        headerRow?.appendChild(actions);

        const termHost = document.createElement('div');
        termHost.className = 'topology-card-mini-terminal';

        slotEl.append(termHost);

        const pane = new TerminalPane(termHost, {
            session: name,
            machine,
            // A card is a compact peek — render smaller than the global terminal
            // pref (16/20px) so more rows are visible; the ⤢ pop-out opens the
            // full-size window for real work.
            fontSize: 14,
            onSessionEnded: () => this._topologyView?.collapseCard(name),
        });

        let transcriptBar = null;
        const removeTranscriptBar = () => {
            transcriptBar?.remove();
            transcriptBar = null;
            pane.focus();
        };

        const sendVoiceText = async (text) => {
            try {
                const res = await apiFetch(`/send/${sessionId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: voicePromptWrap(text) }),
                });
                const data = await res.json();
                if (data.error) throw new Error(data.error);
                pane.setStatus('connected', `Sent: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`);
                setTimeout(() => pane.setStatus('connected', 'Connected'), 3000);
            } catch (err) {
                console.error('[WorkspaceWindow] Voice send failed:', err);
                pane.setStatus('error', err.message || 'Voice input failed');
            }
        };

        const showTranscriptBar = (text) => {
            removeTranscriptBar();
            const bar = document.createElement('div');
            bar.className = 'wb-transcript-bar';
            bar.innerHTML = `
                <input type="text" class="wb-transcript-input" />
                <button class="wb-transcript-send" title="Send (Enter)">➤</button>
                <button class="wb-transcript-dismiss" title="Discard (Esc)">✕</button>
            `;
            const input = bar.querySelector('.wb-transcript-input');
            input.value = text;

            const send = () => {
                const value = input.value.trim();
                removeTranscriptBar();
                if (value) sendVoiceText(value);
            };
            bar.querySelector('.wb-transcript-send').addEventListener('click', send);
            bar.querySelector('.wb-transcript-dismiss').addEventListener('click', removeTranscriptBar);
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); send(); }
                else if (e.key === 'Escape') { e.preventDefault(); removeTranscriptBar(); }
                e.stopPropagation();
            });

            termHost.before(bar);
            transcriptBar = bar;
            input.focus();
            input.select();
        };

        const ptt = new PttController({
            getVoiceStatus: () => desktop.voiceStatus,
            onState: (state) => {
                pttBtn.classList.remove('recording', 'processing');
                if (state === 'recording') {
                    pttBtn.classList.add('recording');
                    pttBtn.querySelector('.ptt-icon').textContent = '🔴';
                } else if (state === 'processing') {
                    pttBtn.classList.add('processing');
                    pttBtn.querySelector('.ptt-icon').textContent = '🎤';
                } else {
                    pttBtn.querySelector('.ptt-icon').textContent = '🎤';
                }
            },
            onResult: (text) => {
                if (isAutoSend()) sendVoiceText(text);
                else showTranscriptBar(text);
            },
            onError: (kind, message) => pane.setStatus('error', message),
        });

        // Same pointer-capture pattern as SessionWindow's titlebar PTT button.
        const onDown = (e) => {
            e.preventDefault();
            e.stopPropagation();
            pttBtn.setPointerCapture?.(e.pointerId);
            ptt.start();
        };
        const onUp = (e) => {
            e.stopPropagation();
            pttBtn.releasePointerCapture?.(e.pointerId);
            if (ptt.state === 'recording') ptt.stop();
        };
        const onCancel = (e) => {
            pttBtn.releasePointerCapture?.(e.pointerId);
            if (ptt.state === 'recording') ptt.cancel();
        };
        pttBtn.addEventListener('pointerdown', onDown, true);
        pttBtn.addEventListener('pointerup', onUp, true);
        pttBtn.addEventListener('pointercancel', onCancel);

        return () => {
            removeTranscriptBar();
            actions.remove();
            pane.dispose();
        };
    }
}
