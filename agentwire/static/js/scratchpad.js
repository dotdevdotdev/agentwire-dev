/**
 * Scratch Pad — slide-in notes drawer docked to the right edge.
 *
 * "Freeze" text on screen: capture a selection via the popover (or write a
 * note by hand) and it stays put in the drawer — above WinBoxes, surviving
 * session switches and reloads. Notes are server-backed
 * (~/.agentwire/scratchpad.json) and shared across portal clients; agents
 * add notes via the scratchpad_add MCP tool.
 *
 * Toggle: Alt+N or the edge handle. Selection popover appears near any DOM
 * text selection (Monitor panes, artifacts, sidebar — xterm canvas
 * selections are not DOM selections and are out of scope for v1).
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';
import { armDeadKeySuppressor } from './dead-key-suppressor.js';

const SAVE_DEBOUNCE_MS = 600;

class ScratchPad {
    constructor() {
        this.drawer = null;
        this.handle = null;
        this.listEl = null;
        this.popover = null;
        this.open = false;
        this.notes = [];
        /** @type {Map<string, number>} note id -> debounce timer */
        this.saveTimers = new Map();
        this.refreshPending = false;
        // xterm selections aren't DOM selections — stashed here by the
        // 'terminal-selection' event until the popover consumes them.
        this.pendingTerminalText = null;
        this.pendingTerminalSource = null;
    }

    init() {
        this._buildDrawer();
        this._buildPopover();

        // Live sync from other clients / CLI / MCP writers
        desktop.on('scratchpad_updated', ({ notes }) => this._onRemoteUpdate(notes));

        // Alt+N toggles the drawer. Capture phase + stopPropagation so xterm
        // never sees the keystroke; e.code because on macOS Option+N is a
        // dead key (e.key is 'Dead', and it starts a ˜ composition that the
        // shared suppressor swallows — see dead-key-suppressor.js).
        window.addEventListener('keydown', (e) => {
            if (e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyN') {
                e.preventDefault();
                e.stopPropagation();
                if (e.repeat) return;
                armDeadKeySuppressor();
                this.toggle();
            }
        }, true);

        // Selection capture popover (DOM selections: Monitor panes, artifacts…)
        document.addEventListener('mouseup', () => this._maybeShowPopover());
        document.addEventListener('selectionchange', () => {
            const sel = window.getSelection();
            if (!sel || sel.isCollapsed) {
                // Don't hide a popover anchored to an xterm selection — those
                // never appear in the DOM selection.
                if (!this.pendingTerminalText) this._hidePopover();
            }
        });

        // xterm selections (canvas-rendered) arrive via session-window.js
        window.addEventListener('terminal-selection', (e) => {
            const { text, x, y, session } = e.detail;
            this.pendingTerminalText = text;
            this.pendingTerminalSource = session || 'terminal';
            this._showPopoverAt(x, y - 38);
        });
        // Any other mousedown clears a terminal-anchored popover
        document.addEventListener('mousedown', (e) => {
            if (this.pendingTerminalText && e.target !== this.popover) {
                this.pendingTerminalText = null;
                this._hidePopover();
            }
        });

        this._fetchNotes();
    }

    // ─── DOM ────────────────────────────────────────────────────

    _buildDrawer() {
        const drawer = document.createElement('div');
        drawer.className = 'scratchpad-drawer';
        drawer.innerHTML = `
            <div class="scratchpad-header">
                <span class="scratchpad-title">Scratch Pad</span>
                <button class="scratchpad-new-btn" title="New note">+ Note</button>
                <button class="scratchpad-close-btn" title="Close (Alt+N)">×</button>
            </div>
            <div class="scratchpad-list"></div>
            <div class="scratchpad-hint">Select text anywhere → 📌 to capture · Alt+N to toggle</div>
        `;
        document.body.appendChild(drawer);
        this.drawer = drawer;
        this.listEl = drawer.querySelector('.scratchpad-list');

        drawer.querySelector('.scratchpad-close-btn').addEventListener('click', () => this.toggle(false));
        drawer.querySelector('.scratchpad-new-btn').addEventListener('click', async () => {
            const note = await this._apiAdd('New note');
            if (note) {
                // Focus the fresh note for immediate editing
                requestAnimationFrame(() => {
                    const ta = this.listEl.querySelector(`[data-note-id="${note.id}"] textarea`);
                    if (ta) { ta.focus(); ta.select(); }
                });
            }
        });

        const handle = document.createElement('button');
        handle.className = 'scratchpad-handle';
        handle.title = 'Scratch pad (Alt+N)';
        handle.innerHTML = '📌';
        handle.addEventListener('click', () => this.toggle());
        document.body.appendChild(handle);
        this.handle = handle;
    }

    _buildPopover() {
        const btn = document.createElement('button');
        btn.className = 'scratchpad-capture-btn';
        btn.innerHTML = '📌 Pin to pad';
        btn.style.display = 'none';
        // mousedown (not click): act before the browser collapses the selection
        btn.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const text = this.pendingTerminalText
                ?? (window.getSelection()?.toString() ?? '');
            const source = this.pendingTerminalText
                ? this.pendingTerminalSource : 'selection';
            this.pendingTerminalText = null;
            this._hidePopover();
            if (text.trim()) this._apiAdd(text, source);
        });
        document.body.appendChild(btn);
        this.popover = btn;
    }

    _maybeShowPopover() {
        if (this.pendingTerminalText) return; // terminal flow owns the popover
        const sel = window.getSelection();
        const text = sel && !sel.isCollapsed ? sel.toString().trim() : '';
        if (!text || this.drawer.contains(sel.anchorNode)) {
            this._hidePopover();
            return;
        }
        let rect;
        try {
            rect = sel.getRangeAt(0).getBoundingClientRect();
        } catch {
            return;
        }
        if (!rect || (rect.width === 0 && rect.height === 0)) return;
        this._showPopoverAt(rect.right - 40, rect.top - 34);
    }

    _showPopoverAt(x, y) {
        const btn = this.popover;
        btn.style.display = 'block';
        btn.style.left = `${Math.min(Math.max(x, 8), window.innerWidth - 120)}px`;
        btn.style.top = `${Math.max(y, 8)}px`;
    }

    _hidePopover() {
        if (this.popover) this.popover.style.display = 'none';
    }

    // ─── State ──────────────────────────────────────────────────

    toggle(force = null) {
        this.open = force ?? !this.open;
        this.drawer.classList.toggle('open', this.open);
        this.handle.classList.toggle('drawer-open', this.open);
    }

    _pulseHandle() {
        this.handle.classList.remove('pulse');
        void this.handle.offsetWidth; // restart the animation
        this.handle.classList.add('pulse');
    }

    _onRemoteUpdate(notes) {
        this.notes = notes;
        // Don't yank the DOM out from under an active edit — refresh on blur.
        const editing = this.drawer.contains(document.activeElement)
            && document.activeElement.tagName === 'TEXTAREA';
        if (editing) {
            this.refreshPending = true;
            return;
        }
        this._render();
    }

    // ─── API ────────────────────────────────────────────────────

    async _fetchNotes() {
        try {
            const resp = await apiFetch('/api/scratchpad');
            if (!resp.ok) return;
            this.notes = (await resp.json()).notes || [];
            this._render();
        } catch { /* portal hiccup — next broadcast will catch us up */ }
    }

    async _apiAdd(text, source = null) {
        try {
            const resp = await apiFetch('/api/scratchpad/notes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, source }),
            });
            if (!resp.ok) return null;
            this._pulseHandle();
            return (await resp.json()).note;
        } catch {
            return null;
        }
    }

    _scheduleSave(id, text) {
        clearTimeout(this.saveTimers.get(id));
        this.saveTimers.set(id, setTimeout(async () => {
            this.saveTimers.delete(id);
            try {
                await apiFetch(`/api/scratchpad/notes/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text }),
                });
            } catch { /* retried implicitly on next edit */ }
        }, SAVE_DEBOUNCE_MS));
    }

    async _apiRemove(id) {
        try {
            await apiFetch(`/api/scratchpad/notes/${id}`, { method: 'DELETE' });
        } catch { /* broadcast will reconcile */ }
    }

    // ─── Render ─────────────────────────────────────────────────

    _render() {
        this.refreshPending = false;
        this.listEl.innerHTML = '';
        if (!this.notes.length) {
            const empty = document.createElement('div');
            empty.className = 'scratchpad-empty';
            empty.textContent = 'No notes yet — select some text and pin it.';
            this.listEl.appendChild(empty);
            return;
        }
        for (const note of this.notes) {
            this.listEl.appendChild(this._renderNote(note));
        }
    }

    _renderNote(note) {
        const card = document.createElement('div');
        card.className = 'scratchpad-note';
        card.dataset.noteId = note.id;

        const ta = document.createElement('textarea');
        ta.value = note.text;
        ta.rows = 1;
        ta.spellcheck = false;
        const autosize = () => {
            ta.style.height = 'auto';
            ta.style.height = `${Math.min(ta.scrollHeight, 320)}px`;
        };
        ta.addEventListener('input', () => {
            autosize();
            this._scheduleSave(note.id, ta.value);
        });
        ta.addEventListener('blur', () => {
            if (this.refreshPending) this._render();
        });
        requestAnimationFrame(autosize);

        const meta = document.createElement('div');
        meta.className = 'scratchpad-note-meta';
        const when = note.updated ? new Date(note.updated).toLocaleString(
            undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
        meta.innerHTML = `
            <span>${note.source ? `${this._esc(note.source)} · ` : ''}${when}</span>
            <button class="scratchpad-note-delete" title="Delete note">×</button>
        `;
        meta.querySelector('.scratchpad-note-delete').addEventListener('click', () => {
            card.remove();
            this._apiRemove(note.id);
        });

        card.appendChild(ta);
        card.appendChild(meta);
        return card;
    }

    _esc(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }
}

export const scratchpad = new ScratchPad();
