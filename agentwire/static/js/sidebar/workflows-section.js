/**
 * Workflows sidebar section — defs-first list with inline run drill-down.
 *
 * Top level is a list of workflow definitions (from /api/workflows/list).
 * Each row carries a status dot (enabled/disabled/never-scheduled), a
 * schedule cadence pill, and a chevron. Click expands the row inline and
 * lazy-fetches that workflow's recent runs from /api/workflows/runs?workflow=X.
 * Click a run row → opens the WorkflowWindow detail view.
 *
 * Orphan scheduler tasks (workflow: refs without a matching def) render as
 * red warning rows above the def list so broken config is visible.
 */

import { openWorkflowWindow } from '../windows/workflow-window.js';

const EXPANDED_KEY = 'workflows-section-expanded';

function _loadExpanded() {
    try {
        const raw = localStorage.getItem(EXPANDED_KEY);
        if (!raw) return new Set();
        const arr = JSON.parse(raw);
        return new Set(Array.isArray(arr) ? arr : []);
    } catch {
        return new Set();
    }
}

function _saveExpanded(set) {
    try {
        localStorage.setItem(EXPANDED_KEY, JSON.stringify([...set]));
    } catch {
        // localStorage may be unavailable; expansions stay session-scoped
    }
}

function _fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
        + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function _fmtDuration(ms) {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return rem ? `${m}m${rem}s` : `${m}m`;
}

function _runStatusDot(status) {
    if (status === 'success') return 'dot-online';
    if (status === 'failed' || status === 'error') return 'dot-offline';
    if (status === 'running') return 'dot-processing';
    return 'dot-idle';
}

function _defStatusDot(wf) {
    if (!wf.scheduled) return 'dot-checking';
    return wf.enabled ? 'dot-online' : 'dot-idle';
}

function _escape(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

function _sortWorkflows(workflows) {
    // Active first (scheduled && enabled), then scheduled-but-disabled, then
    // unscheduled defs. Alpha within each group.
    const rank = wf => {
        if (wf.scheduled && wf.enabled) return 0;
        if (wf.scheduled) return 1;
        return 2;
    };
    return [...workflows].sort((a, b) => {
        const ra = rank(a), rb = rank(b);
        if (ra !== rb) return ra - rb;
        return a.name.localeCompare(b.name);
    });
}

export const workflowsSection = {
    title: 'Workflows',
    autoRefreshMs: 10000,
    _body: null,
    _workflows: [],
    _orphans: [],
    _expanded: null,         // Set<string> hydrated lazily on mount
    _runsCache: new Map(),   // workflow_name -> runs[] (most recent fetch)

    actions: [
        { id: 'refresh', label: '↻', title: 'Refresh' },
    ],

    onAction(actionId, body) {
        if (actionId === 'refresh') this.refresh(body);
    },

    async mount(body) {
        this._body = body;
        if (this._expanded === null) this._expanded = _loadExpanded();
        await this.refresh(body);
    },

    async refresh(body) {
        this._body = body;
        if (this._expanded === null) this._expanded = _loadExpanded();
        try {
            const res = await fetch('/api/workflows/list');
            const data = await res.json();
            if (data.error) throw new Error(data.error);
            this._workflows = Array.isArray(data.workflows) ? data.workflows : [];
            this._orphans = Array.isArray(data.orphans) ? data.orphans : [];
        } catch {
            this._workflows = null;
            this._orphans = [];
        }

        // Refresh run lists for any expanded workflows in parallel.
        if (this._workflows) {
            const known = new Set(this._workflows.map(w => w.name));
            // Drop expansions whose def no longer exists.
            for (const name of [...this._expanded]) {
                if (!known.has(name)) this._expanded.delete(name);
            }
            _saveExpanded(this._expanded);
            await Promise.all([...this._expanded].map(name => this._fetchRuns(name)));
        }

        this._render(body);
    },

    async _fetchRuns(workflowName) {
        try {
            const res = await fetch(`/api/workflows/runs?workflow=${encodeURIComponent(workflowName)}&limit=30`);
            const data = await res.json();
            this._runsCache.set(workflowName, Array.isArray(data.runs) ? data.runs : []);
        } catch {
            this._runsCache.set(workflowName, []);
        }
    },

    _render(body) {
        if (this._workflows === null) {
            body.innerHTML = '<div class="sidebar-empty">Failed to load workflows</div>';
            return;
        }
        if (this._workflows.length === 0 && this._orphans.length === 0) {
            body.innerHTML = '<div class="sidebar-empty">No workflows defined</div>';
            return;
        }

        const parts = [];

        // Orphans first — scheduler tasks pointing at deleted defs.
        for (const orph of this._orphans) {
            const cadence = orph.schedule_summary
                ? `<span class="sidebar-tag">${_escape(orph.schedule_summary)}</span>`
                : '';
            parts.push(`<div class="sidebar-list-item sidebar-workflow-orphan" data-orphan="${_escape(orph.name)}" title="Scheduler task '${_escape(orph.task_name)}' references missing workflow def">
                <span class="sidebar-status-dot dot-offline"></span>
                <span class="sidebar-list-item-title">${_escape(orph.name)}</span>
                ${cadence}
                <span class="sidebar-tag sidebar-tag-warn">missing def</span>
            </div>`);
        }

        // Workflow defs, sorted active-first then alphabetical.
        const sorted = _sortWorkflows(this._workflows);
        for (const wf of sorted) {
            const expanded = this._expanded.has(wf.name);
            const dot = _defStatusDot(wf);
            const cadence = wf.schedule_summary
                ? `<span class="sidebar-tag">${_escape(wf.schedule_summary)}</span>`
                : (wf.scheduled ? '' : '<span class="sidebar-tag">unscheduled</span>');
            parts.push(`<div class="sidebar-list-item sidebar-workflow-def" data-workflow="${_escape(wf.name)}" data-expanded="${expanded}" title="${_escape(wf.description || wf.name)}">
                <span class="sidebar-status-dot ${dot}"></span>
                <span class="sidebar-list-item-title">${_escape(wf.name)}</span>
                ${cadence}
                <span class="sidebar-chevron">▸</span>
            </div>`);

            if (expanded) {
                parts.push(this._renderRuns(wf.name));
            }
        }

        body.innerHTML = parts.join('');
        body.onclick = (e) => this._handleClick(e);
    },

    _renderRuns(workflowName) {
        const runs = this._runsCache.get(workflowName);
        if (runs === undefined) {
            return `<div class="sidebar-workflow-runs" data-parent="${_escape(workflowName)}">
                <div class="sidebar-empty">Loading…</div>
            </div>`;
        }
        if (runs.length === 0) {
            return `<div class="sidebar-workflow-runs" data-parent="${_escape(workflowName)}">
                <div class="sidebar-empty">No runs yet</div>
            </div>`;
        }
        const rows = runs.map(r => {
            const dot = _runStatusDot(r.status);
            const when = _fmtTime(r.started_at);
            const dur = _fmtDuration(r.duration_ms);
            const runner = r.runner || '';
            const runnerBadge = runner
                ? `<span class="sidebar-workflow-runner" data-runner="${_escape(runner)}">${_escape(runner)}</span>`
                : '';
            return `<div class="sidebar-list-item sidebar-workflow-run" data-run-id="${_escape(r.run_id)}" title="${_escape(r.run_id)}">
                <span class="sidebar-status-dot ${dot}"></span>
                <span class="sidebar-list-item-title">${_escape(when)} · ${_escape(dur)}</span>
                ${runnerBadge}
            </div>`;
        }).join('');
        return `<div class="sidebar-workflow-runs" data-parent="${_escape(workflowName)}">${rows}</div>`;
    },

    _handleClick(e) {
        const runItem = e.target.closest('[data-run-id]');
        if (runItem) {
            openWorkflowWindow(runItem.dataset.runId);
            return;
        }
        const defItem = e.target.closest('[data-workflow]');
        if (defItem) {
            this._toggleExpand(defItem.dataset.workflow);
            return;
        }
        // Orphan rows are non-interactive for now.
    },

    async _toggleExpand(workflowName) {
        if (this._expanded.has(workflowName)) {
            this._expanded.delete(workflowName);
            _saveExpanded(this._expanded);
            this._render(this._body);
            return;
        }
        this._expanded.add(workflowName);
        _saveExpanded(this._expanded);
        // Render immediately with the loading placeholder, then patch in runs.
        this._render(this._body);
        if (!this._runsCache.has(workflowName)) {
            await this._fetchRuns(workflowName);
            this._render(this._body);
        }
    },
};
