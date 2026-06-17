/**
 * Council sidebar section — lists live sittings and opens the board window.
 *
 * The board itself (grid, deltas, reader) lives in council-window.js; this is
 * just the launcher + a compact "N of M in" status line per live sitting.
 */

import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';
import { openCouncilWindow } from '../council-window.js';

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

export const councilSection = {
    title: 'Council',
    _body: null,
    _sittings: [],

    async mount(body) {
        this._body = body;
        // A board delta is a cheap signal that something changed — refresh the
        // compact counts (debounced by the natural delta cadence).
        desktop.on('council_update', () => this.refresh(body));
        await this.refresh(body);
    },

    async refresh(body) {
        try {
            const res = await apiFetch('/api/council/sittings');
            const data = res.ok ? await res.json() : { sittings: [] };
            this._sittings = data.sittings || [];
        } catch {
            this._sittings = [];
        }
        // Per-sitting live counts (best-effort; ignore failures).
        const counts = {};
        await Promise.all(this._sittings.map(async (name) => {
            try {
                const r = await apiFetch(`/api/council/live?sitting=${encodeURIComponent(name)}`);
                if (r.ok) {
                    const s = await r.json();
                    counts[name] = { final: s.final, total: s.total, prompt: s.prompt_text };
                }
            } catch { /* ignore */ }
        }));
        this._render(body, counts);
    },

    _render(body, counts) {
        if (!this._sittings.length) {
            body.innerHTML = '<div class="sidebar-empty">No council sittings.</div>';
            return;
        }
        body.innerHTML = `
            <div class="council-section-list">
                ${this._sittings.map((name) => {
                    const c = counts[name];
                    const meta = c
                        ? `<span class="council-section-count">${c.final} of ${c.total} in</span>`
                        : '';
                    return `
                        <button class="council-section-item" data-sitting="${esc(name)}">
                            <span class="council-section-name">${esc(name)}</span>
                            ${meta}
                        </button>`;
                }).join('')}
            </div>`;
        body.querySelectorAll('.council-section-item').forEach((btn) => {
            btn.addEventListener('click', () => openCouncilWindow(btn.dataset.sitting));
        });
    },
};
