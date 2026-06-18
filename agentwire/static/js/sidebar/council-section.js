/**
 * Council sidebar section — live sittings + an archive of past threads.
 *
 * Live sittings show a compact "N of M in" status; archived threads (dismissed
 * but preserved on disk) list under an Archive subhead and reopen the board
 * read-only, where they can be re-asked (which re-seats the same roster). The
 * board itself (grid, deltas, reader) lives in council-window.js; this is the
 * launcher + index.
 */

import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

/** First line of a question, clamped — the thread's at-a-glance label. */
function snippet(text, max = 60) {
    const line = String(text || '').trim().split('\n')[0];
    return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

export const councilSection = {
    title: 'Council',
    _body: null,
    _sittings: [],
    _archive: [],

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
        try {
            const res = await apiFetch('/api/council/archive');
            const data = res.ok ? await res.json() : { archive: [] };
            this._archive = data.archive || [];
        } catch {
            this._archive = [];
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
        const liveHtml = this._sittings.map((name) => {
            const c = counts[name];
            const meta = c
                ? `<span class="council-section-count">${c.final} of ${c.total} in</span>`
                : '';
            return `
                <button class="council-section-item" data-sitting="${esc(name)}">
                    <span class="council-section-name">${esc(name)}</span>
                    ${meta}
                </button>`;
        }).join('');

        const archiveHtml = this._archive.length ? `
            <div class="council-section-subhead">Archive</div>
            ${this._archive.map((a) => `
                <button class="council-section-item council-section-item--archived" data-sitting="${esc(a.name)}" title="${esc(snippet(a.last_prompt_text, 200))}">
                    <span class="council-section-name">${esc(a.name)}</span>
                    <span class="council-section-snip">${esc(snippet(a.last_prompt_text))}</span>
                    <span class="council-section-count">${a.rounds} round${a.rounds === 1 ? '' : 's'}</span>
                </button>`).join('')}
        ` : '';

        // Always offer the workspace launcher (seat/ask a fresh council there).
        body.innerHTML = `
            <div class="council-section-list">
                <button class="council-section-item council-section-item--open" data-open-empty="1">
                    <span class="council-section-name">Open council</span>
                    <span class="council-section-count">seat &amp; ask</span>
                </button>
                ${liveHtml}
                ${archiveHtml}
            </div>`;

        body.querySelector('[data-open-empty]')?.addEventListener('click', async () => {
            const { openCouncilWindow } = await import('../desktop.js');
            openCouncilWindow(null);
        });
        body.querySelectorAll('.council-section-item[data-sitting]').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const { openCouncilWindow } = await import('../desktop.js');
                openCouncilWindow(btn.dataset.sitting);
            });
        });
    },
};
