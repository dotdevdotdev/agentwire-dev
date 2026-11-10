/**
 * Approvals sidebar section — every pending permission ask (#1343).
 *
 * Pure renderer over GET /api/permissions, so an ask survives a missed
 * broadcast. Allow/Deny send the ask's request_id: with two or more pending
 * asks on one session the server refuses an id-less respond.
 */

import { apiFetch } from '../api.js';

const POLL_MS = 3000;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

function exactText(p) {
    const input = p.tool_input || {};
    if (p.tool_name === 'Bash') return input.command || '';
    if (input.file_path) return `${p.tool_name} ${input.file_path}`;
    return `${p.tool_name} ${JSON.stringify(input)}`.slice(0, 400);
}

export const approvalsSection = {
    title: 'Approvals',
    _timer: null,

    async mount(body) {
        body.addEventListener('click', (e) => this._onClick(e, body));
        await this.refresh(body);
        clearInterval(this._timer);
        this._timer = setInterval(() => this.refresh(body), POLL_MS);
    },

    async refresh(body) {
        let data = null;
        try {
            const res = await apiFetch('/api/permissions');
            data = res.ok ? await res.json() : null;
        } catch {
            data = null;
        }
        if (!data) {
            body.innerHTML = '<div class="approval-empty">Approvals unavailable</div>';
            return;
        }
        this._render(body, data.pending || []);
    },

    _render(body, pending) {
        if (!pending.length) {
            body.innerHTML = '<div class="approval-empty">Nothing waiting</div>';
            return;
        }
        body.innerHTML = pending.map((p) => `
            <div class="approval-card">
                <div class="approval-summary">${esc(p.summary)}</div>
                <div class="approval-detail">${esc(p.agent)} · ${esc(p.session)}\n${esc(exactText(p))}</div>
                <div class="approval-actions">
                    <button class="approval-btn" data-decision="allow"
                        data-session="${esc(p.session)}" data-request="${esc(p.request_id)}">Allow</button>
                    <button class="approval-btn approval-btn--deny" data-decision="deny"
                        data-session="${esc(p.session)}" data-request="${esc(p.request_id)}">Deny</button>
                </div>
            </div>`).join('');
    },

    async _onClick(e, body) {
        const btn = e.target.closest('.approval-btn');
        if (!btn) return;
        btn.disabled = true;
        try {
            await apiFetch(`/api/permission/${encodeURIComponent(btn.dataset.session)}/respond`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    decision: btn.dataset.decision,
                    request_id: btn.dataset.request,
                }),
            });
        } catch (err) {
            console.error('[approvals] respond failed', err);
        }
        await this.refresh(body);
    },
};
