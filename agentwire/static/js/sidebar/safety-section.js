/**
 * Safety sidebar section — compact damage-control review.
 *
 * Caps the event list to SIDEBAR_LIMIT and offers a "Show all" button that
 * opens a maximized WinBox window with the full review tool.
 */

import {
    eventRow,
    escapeHtml,
    fetchSafetyStatus,
    fetchSafetyLogs,
    showEventModal,
} from '../safety-shared.js';

const HOST_MANAGED_NOTE = 'Managed in <code>~/.agentwire/config.yaml</code> — frozen from the API (#425).';

const SIDEBAR_LIMIT = 10;

let currentFilter = { decision: '' };

function renderHeader(status) {
    const enabled = status?.enabled ?? true;
    const counts = status?.today_counts || {};
    const fmt = (k) => counts[k] || 0;
    return `<div class="safety-header">
        <div class="safety-badge ${enabled ? 'on' : 'off'}">
            <span class="safety-badge-dot">${enabled ? '●' : '○'}</span>
            <span>Damage control ${enabled ? 'ON' : 'OFF'}</span>
        </div>
        <div class="safety-managed-note">${HOST_MANAGED_NOTE}</div>
        <div class="safety-stats">
            <span class="safety-decision blocked">${fmt('blocked')}</span>
            <span class="safety-decision escape">${fmt('allowed_by_escape')}</span>
            <span class="safety-decision asked">${fmt('asked')}</span>
            <span class="safety-decision disabled-mode">${fmt('allowed_by_disabled')}</span>
        </div>
        <div class="safety-filters">
            <button class="safety-chip ${currentFilter.decision === '' ? 'active' : ''}" data-decision="">all</button>
            <button class="safety-chip ${currentFilter.decision === 'blocked' ? 'active' : ''}" data-decision="blocked">blocked</button>
            <button class="safety-chip ${currentFilter.decision === 'allowed_by_escape' ? 'active' : ''}" data-decision="allowed_by_escape">escape</button>
            <button class="safety-chip ${currentFilter.decision === 'asked' ? 'active' : ''}" data-decision="asked">asked</button>
        </div>
    </div>`;
}

function renderDisabledRules(status) {
    const disabled = status?.disabled_rules || [];
    return `<div class="safety-disabled-rules">
        <div class="safety-disabled-rules-header">
            <span>Disabled rules (${disabled.length})</span>
        </div>
        <div class="safety-disabled-rules-list">
            ${disabled.length ? disabled.map((id) => `
                <div class="safety-disabled-rule" data-rule-id="${escapeHtml(id)}">
                    <code>${escapeHtml(id)}</code>
                </div>
            `).join('') : '<div class="safety-empty">No rules disabled.</div>'}
        </div>
    </div>`;
}

export const safetySection = {
    title: 'Safety',
    _body: null,
    async mount(body) {
        this._body = body;
        await this.refresh(body);
    },
    async refresh(body) {
        body.innerHTML = '<div class="sidebar-empty">Loading…</div>';
        const [status, allEntries] = await Promise.all([
            fetchSafetyStatus(),
            fetchSafetyLogs(currentFilter.decision, 500),
        ]);

        const newestFirst = allEntries.slice().reverse();
        const shown = newestFirst.slice(0, SIDEBAR_LIMIT);
        const remaining = newestFirst.length - shown.length;

        body.innerHTML = renderHeader(status)
            + renderDisabledRules(status)
            + (shown.length
                ? shown.map(eventRow).join('')
                : '<div class="sidebar-empty">No safety events yet.</div>')
            + (remaining > 0
                ? `<button class="safety-show-all" data-action="open-window">Show all ${newestFirst.length} events →</button>`
                : '');

        body.querySelectorAll('.safety-chip').forEach((chip) => {
            chip.addEventListener('click', () => {
                currentFilter.decision = chip.dataset.decision || '';
                this.refresh(body);
            });
        });
        body.querySelectorAll('.safety-event').forEach((row) => {
            row.addEventListener('click', () => {
                try {
                    showEventModal(JSON.parse(row.dataset.event));
                } catch (_) {}
            });
        });
        body.querySelector('[data-action="open-window"]')?.addEventListener('click', async () => {
            const { openSafetyWindow } = await import('../safety-window.js');
            const { sidebar } = await import('../sidebar.js');
            // Close the sidebar so the maximized window has full screen real estate.
            try { sidebar.unpin(); } catch (_) {}
            try { sidebar.close(); } catch (_) {}
            openSafetyWindow();
        });
    },
};
