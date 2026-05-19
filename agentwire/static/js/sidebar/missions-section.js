/**
 * Missions sidebar section.
 *
 * Shows two lists per repo: active worker sessions and eligible-but-unstarted
 * issues. Action buttons let the user fire one dispatcher tick or gc run
 * without leaving the portal. Listens for ``mission_changed`` WS events from
 * the server and refreshes automatically.
 */

import { desktop } from '../desktop-manager.js';

export const missionsSection = {
    title: 'Missions',
    actions: [
        { id: 'tick', label: '⚡', title: 'Run one dispatcher tick now' },
        { id: 'gc', label: '🧹', title: 'Run worktree gc now' },
        { id: 'refresh', label: '↻', title: 'Refresh' },
    ],
    autoRefreshMs: 60_000,

    _state: null,
    _busy: false,

    async mount(body) {
        desktop.on('mission_changed', () => this.refresh(body));
        await this.refresh(body);
    },

    async refresh(body) {
        try {
            const res = await fetch('/api/missions/list');
            this._state = res.ok ? await res.json() : null;
        } catch (e) {
            this._state = null;
        }
        this._render(body);
    },

    async onAction(action, body) {
        if (this._busy) return;
        if (action === 'refresh') {
            await this.refresh(body);
            return;
        }
        this._busy = true;
        const endpoint = action === 'tick' ? '/api/missions/tick' : '/api/missions/gc';
        try {
            await fetch(endpoint, { method: 'POST' });
        } catch (e) {
            console.warn('mission action failed', action, e);
        } finally {
            this._busy = false;
        }
        await this.refresh(body);
    },

    _render(body) {
        const state = this._state;
        if (!state) {
            body.innerHTML = '<div class="sidebar-empty">Mission data unavailable</div>';
            return;
        }

        const active = state.active || {};
        const eligible = state.eligible || {};
        const errors = state.errors || [];

        let html = '';

        // Active workers
        const repos = new Set([...Object.keys(active), ...Object.keys(eligible)]);
        const activeTotal = Object.values(active)
            .reduce((sum, rows) => sum + (rows?.length || 0), 0);
        html += `<div class="sidebar-section-subheader">Active (${activeTotal})</div>`;
        if (!activeTotal) {
            html += '<div class="sidebar-empty-inline">No mission workers running</div>';
        } else {
            for (const repo of repos) {
                const rows = active[repo] || [];
                if (!rows.length) continue;
                html += `<div class="sidebar-section-subheader-small">${escape(repo)}</div>`;
                for (const row of rows) {
                    html += `<div class="sidebar-list-item" data-mission-session="${escape(row.session)}">
                        <span class="sidebar-status-dot dot-processing"></span>
                        <span class="sidebar-list-item-title">#${row.issue} ${escape(row.slug)}</span>
                    </div>`;
                }
            }
        }

        // Eligible queue
        let eligibleTotal = 0;
        const eligibleRows = {};
        for (const repo of Object.keys(eligible)) {
            const rows = (eligible[repo] || []).filter(r => r.eligible);
            eligibleRows[repo] = rows;
            eligibleTotal += rows.length;
        }
        html += `<div class="sidebar-section-subheader">Eligible queue (${eligibleTotal})</div>`;
        if (!eligibleTotal) {
            html += '<div class="sidebar-empty-inline">No agent-ready issues</div>';
        } else {
            for (const repo of Object.keys(eligibleRows)) {
                const rows = eligibleRows[repo];
                if (!rows.length) continue;
                html += `<div class="sidebar-section-subheader-small">${escape(repo)}</div>`;
                for (const row of rows) {
                    html += `<div class="sidebar-list-item">
                        <span class="sidebar-status-dot dot-idle"></span>
                        <span class="sidebar-list-item-title">#${row.issue} ${escape(row.title)}</span>
                    </div>`;
                }
            }
        }

        if (errors.length) {
            html += `<div class="sidebar-section-subheader">Errors</div>`;
            for (const err of errors) {
                html += `<div class="sidebar-list-item sidebar-error">
                    <span class="sidebar-list-item-title">${escape(err.repo)}: ${escape(err.error)}</span>
                </div>`;
            }
        }

        body.innerHTML = html;
    },
};

function escape(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
