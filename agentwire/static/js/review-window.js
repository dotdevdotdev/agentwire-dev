/**
 * review-window.js
 *
 * ReviewWindow — a mobile-first WinBox window that shows a session's git diff
 * (from `agentwire diff`, via GET /api/review/{session}) as collapsible,
 * syntax-light hunks, plus an Approve / Request-changes bar that drives the
 * existing guarded prompt-answer path (POST /api/review/{session}/answer →
 * prompt_router.answer). One tap approves; the compare-and-send guard makes a
 * stale tap a safe no-op.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';
import { toastSuccess, toastError } from './toast.js';

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

export class ReviewWindow {
    /**
     * @param {Object} options
     * @param {string} options.session - Session whose diff to review
     * @param {string} options.windowId - Unique window identifier
     * @param {HTMLElement} options.root - Parent element for WinBox
     * @param {Function} options.onClose - Callback when window closes
     * @param {Function} options.onFocus - Callback when window gains focus
     */
    constructor(options) {
        this.session = options.session;
        this.windowId = options.windowId;
        this.root = options.root || document.body;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;

        this.winbox = null;
        this.isOpen = false;
        this._prompt = null;     // live prompt {kind, question, options, expect, pane}
        this._collapsed = new Set();  // file paths collapsed by the user
    }

    open() {
        if (this.isOpen) { this.focus(); return; }
        const container = this._createContainer();
        this._createWinBox(container);
        this.isOpen = true;
        this.refresh();
    }

    close() {
        if (!this.isOpen) return;
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

    _content() {
        return this.winbox ? this.winbox.body.querySelector('.review-window-content') : null;
    }

    _createContainer() {
        const container = document.createElement('div');
        container.className = 'review-window-content';
        container.innerHTML = `
            <div class="review-toolbar">
                <span class="review-session" title="${esc(this.session)}">${esc(this.session)}</span>
                <span class="review-summary"></span>
                <button class="review-btn review-refresh" title="Reload diff">↻</button>
            </div>
            <div class="review-body"><div class="review-loading">Loading diff…</div></div>
            <div class="review-actions hidden"></div>
        `;
        container.querySelector('.review-refresh').addEventListener('click', () => this.refresh());
        container.addEventListener('click', (e) => this._onClick(e));
        return container;
    }

    _createWinBox(container) {
        this.winbox = new WinBox({
            title: `Review · ${this.session}`,
            icon: '<span style="font-size:14px">&#x1F50D;</span>',
            mount: container,
            root: this.root,
            width: '80%',
            height: '80%',
            x: 'center',
            y: 'center',
            minwidth: 300,
            minheight: 240,
            class: ['review-window'],
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

    async refresh() {
        const content = this._content();
        if (!content) return;
        const body = content.querySelector('.review-body');
        body.innerHTML = '<div class="review-loading">Loading diff…</div>';
        try {
            const res = await apiFetch(`/api/review/${encodeURIComponent(this.session)}`);
            const data = await res.json();
            if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
            this._prompt = data.prompt || null;
            this._renderDiff(content, data.diff || {});
            this._renderActions(content);
        } catch (e) {
            body.innerHTML = `<div class="review-error">Failed to load diff: ${esc(e.message)}</div>`;
        }
    }

    _renderDiff(content, diff) {
        const files = diff.files || [];
        const summary = content.querySelector('.review-summary');
        summary.textContent = files.length
            ? `${files.length} file${files.length > 1 ? 's' : ''} · +${diff.additions || 0} −${diff.deletions || 0} · vs ${diff.base || '?'}`
            : `No changes vs ${diff.base || '?'}`;

        const body = content.querySelector('.review-body');
        if (!files.length) {
            body.innerHTML = '<div class="review-empty">Nothing to review — working tree matches the base.</div>';
            return;
        }
        body.innerHTML = files.map((f) => this._renderFile(f)).join('') +
            (diff.truncated ? '<div class="review-truncated">Diff truncated — review the rest at a desk.</div>' : '');
    }

    _renderFile(f) {
        const path = f.path || f.old_path || '(unknown)';
        const collapsed = this._collapsed.has(path);
        const renamed = f.status === 'renamed' && f.old_path && f.old_path !== f.path;
        const title = renamed ? `${esc(f.old_path)} → ${esc(f.path)}` : esc(path);
        const hunks = f.binary
            ? '<div class="review-binary">Binary file — no text diff.</div>'
            : (f.hunks || []).map((h) => this._renderHunk(h)).join('');
        return `<div class="review-file" data-path="${esc(path)}">
            <div class="review-file-head" data-action="toggle-file">
                <span class="review-caret">${collapsed ? '▸' : '▾'}</span>
                <span class="review-status review-status-${esc(f.status)}">${esc((f.status || '').slice(0, 3))}</span>
                <span class="review-file-path">${title}</span>
                <span class="review-file-stat">+${f.additions || 0} −${f.deletions || 0}</span>
            </div>
            <div class="review-hunks ${collapsed ? 'hidden' : ''}">${hunks}</div>
        </div>`;
    }

    _renderHunk(h) {
        const rows = (h.lines || []).map((ln) => {
            const cls = ln.type === 'add' ? 'add' : ln.type === 'del' ? 'del' : 'ctx';
            const oldN = ln.type === 'add' ? '' : (ln.old_n ?? '');
            const newN = ln.type === 'del' ? '' : (ln.new_n ?? '');
            const sign = ln.type === 'add' ? '+' : ln.type === 'del' ? '−' : ' ';
            return `<div class="review-line review-line-${cls}">
                <span class="review-gutter">${oldN}</span>
                <span class="review-gutter">${newN}</span>
                <span class="review-code"><span class="review-sign">${sign}</span>${esc(ln.content)}</span>
            </div>`;
        }).join('');
        const section = h.section ? ` <span class="review-hunk-section">${esc(h.section)}</span>` : '';
        return `<div class="review-hunk">
            <div class="review-hunk-head">${esc(h.header)}${section}</div>
            ${rows}
        </div>`;
    }

    _renderActions(content) {
        const bar = content.querySelector('.review-actions');
        if (!this._prompt) {
            bar.classList.add('hidden');
            bar.innerHTML = '';
            return;
        }
        const p = this._prompt;
        bar.classList.remove('hidden');
        bar.innerHTML = `
            <div class="review-prompt">
                <span class="review-prompt-kind">${esc(p.kind)}</span>
                <span class="review-prompt-q">${esc(p.question)}</span>
            </div>
            <div class="review-action-buttons">
                <button class="review-btn review-deny" data-action="deny">Request changes</button>
                <button class="review-btn review-approve" data-action="approve">Approve</button>
            </div>`;
    }

    _onClick(e) {
        const toggle = e.target.closest('[data-action="toggle-file"]');
        if (toggle) {
            const path = toggle.closest('.review-file')?.dataset.path;
            if (path) {
                if (this._collapsed.has(path)) this._collapsed.delete(path);
                else this._collapsed.add(path);
                const file = toggle.closest('.review-file');
                file.querySelector('.review-hunks')?.classList.toggle('hidden');
                const caret = file.querySelector('.review-caret');
                if (caret) caret.textContent = this._collapsed.has(path) ? '▸' : '▾';
            }
            return;
        }
        const action = e.target.closest('[data-action="approve"], [data-action="deny"]');
        if (action) this._answer(action.dataset.action);
    }

    async _answer(decision) {
        if (!this._prompt) return;
        const content = this._content();
        const buttons = content.querySelectorAll('.review-action-buttons button');
        buttons.forEach((b) => { b.disabled = true; });
        try {
            const res = await apiFetch(`/api/review/${encodeURIComponent(this.session)}/answer`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    decision,
                    expect: this._prompt.expect,
                    pane: this._prompt.pane || 0,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.success === false) {
                throw new Error(data.message || data.error || `HTTP ${res.status}`);
            }
            toastSuccess(decision === 'approve' ? 'Approved' : 'Requested changes');
            this._prompt = null;
            this._renderActions(content);
        } catch (e) {
            toastError(`Could not ${decision}: ${e.message}`);
            buttons.forEach((b) => { b.disabled = false; });
        }
    }
}
