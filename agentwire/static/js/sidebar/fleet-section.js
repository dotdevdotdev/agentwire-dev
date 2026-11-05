/**
 * Fleet sidebar section — every orchestrator's children and what is stuck (#1277).
 *
 * Pure renderer over GET /api/fleet, which is `agentwire fleet --all --json`.
 * Flags are decided by the CLI (agentwire/fleet.py); nothing is derived here.
 */

import { apiFetch } from '../api.js';

const POLL_MS = 30000;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

export const fleetSection = {
    title: 'Fleet',
    _timer: null,

    async mount(body) {
        await this.refresh(body);
        clearInterval(this._timer);
        this._timer = setInterval(() => this.refresh(body), POLL_MS);
    },

    async refresh(body) {
        let data = null;
        try {
            const res = await apiFetch('/api/fleet');
            data = res.ok ? await res.json() : null;
        } catch {
            data = null;
        }
        if (!data) {
            body.innerHTML = '<div class="fleet-section-empty">Fleet unavailable</div>';
            return;
        }
        this._render(body, data);
    },

    _render(body, data) {
        const parents = data.parents || [];
        if (!parents.length) {
            body.innerHTML = '<div class="fleet-section-empty">No fan-outs</div>';
            return;
        }
        body.innerHTML = parents.map((p) => {
            const kids = (p.children || []).map((c) => {
                const flags = (c.flags || []).map((f) => `<span class="fleet-flag">${esc(f)}</span>`).join('');
                const pr = c.pr ? ` · PR ${esc(c.pr.state)}` : '';
                return `
                    <div class="fleet-child fleet-child--${esc(c.state)}${c.flags?.length ? ' fleet-child--flagged' : ''}">
                        <span class="fleet-child-name">${esc(c.session)}</span>
                        <span class="fleet-child-meta">${esc(c.state)}${pr}</span>
                        ${flags}
                    </div>`;
            }).join('');
            const note = p.ledger === 'ok' ? '' : `<span class="fleet-flag">ledger ${esc(p.ledger)}</span>`;
            return `
                <div class="fleet-parent">
                    <div class="fleet-parent-name">${esc(p.parent)}${p.live ? '' : ' (not live)'} ${note}</div>
                    ${kids || '<div class="fleet-section-empty">no children</div>'}
                </div>`;
        }).join('');
    },
};
