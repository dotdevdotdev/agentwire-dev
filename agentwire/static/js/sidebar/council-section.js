/**
 * Council sidebar section — lists live sittings and opens the board window.
 *
 * The board itself (grid, deltas, reader) lives in council-window.js; this is
 * just the launcher + a compact "N of M in" status line per live sitting.
 */

import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';

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
            // No live sitting — still offer the workspace so it can be seated there.
            body.innerHTML = `
                <div class="council-section-list">
                    <button class="council-section-item" data-open-empty="1">
                        <span class="council-section-name">Open council</span>
                        <span class="council-section-count">seat &amp; ask</span>
                    </button>
                </div>`;
            body.querySelector('[data-open-empty]')?.addEventListener('click', async () => {
                const { openCouncilWindow } = await import('../desktop.js');
                openCouncilWindow(null);
            });
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
            btn.addEventListener('click', async () => {
                const { openCouncilWindow } = await import('../desktop.js');
                openCouncilWindow(btn.dataset.sitting);
            });
        });
    },
};
