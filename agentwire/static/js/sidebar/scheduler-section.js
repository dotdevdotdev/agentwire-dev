import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';
import { toastSuccess, toastError, withButtonBusy, errorFromResponse } from '../toast.js';

const COLLAPSED_KEY = 'scheduler-section-collapsed';
const DEFAULT_COLLAPSED = ['inactive'];  // first-load: hide the long disabled list

function _loadCollapsed() {
    try {
        const raw = localStorage.getItem(COLLAPSED_KEY);
        if (!raw) return new Set(DEFAULT_COLLAPSED);
        const arr = JSON.parse(raw);
        return new Set(Array.isArray(arr) ? arr : DEFAULT_COLLAPSED);
    } catch {
        return new Set(DEFAULT_COLLAPSED);
    }
}

function _saveCollapsed(set) {
    try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
    } catch {
        // localStorage unavailable — collapse state stays session-scoped
    }
}

function _escape(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

export const schedulerSection = {
    title: 'Scheduler',
    _body: null,
    _state: null,
    _collapsed: null,  // Set<string> hydrated lazily on mount
    _lastHtml: '',     // dedupe identical re-renders (kills WS-driven flicker)

    async mount(body) {
        this._body = body;
        if (this._collapsed === null) this._collapsed = _loadCollapsed();

        desktop.on('scheduler_state', (state) => {
            // Merge instead of overwrite — WebSocket events carry status fields
            // but not the task list, so overwriting wipes tasks fetched by
            // refresh() and the sidebar collapses to a bare status indicator.
            this._state = this._state ? Object.assign(this._state, state) : state;
            this._render(body);
        });
        desktop.on('scheduler_update', (update) => {
            if (this._state) {
                Object.assign(this._state, update);
                this._render(body);
            }
        });

        await this.refresh(body);
    },

    async refresh(body) {
        if (this._collapsed === null) this._collapsed = _loadCollapsed();
        try {
            const [liveRes, boardRes] = await Promise.all([
                apiFetch('/api/scheduler/live'),
                apiFetch('/api/scheduler/board'),
            ]);
            const live = liveRes.ok ? await liveRes.json() : null;
            const board = boardRes.ok ? await boardRes.json() : null;
            // The board (tasks, overdue, last status) is available whether or not
            // the daemon is running. Render it regardless so a stopped scheduler
            // still shows what's queued + how overdue it is, plus a start toggle.
            if (board) {
                this._state = { running: !!live, ...(live || {}), tasks: board.tasks || [] };
            } else {
                // No board at all — API unreachable, not just a stopped daemon.
                this._state = null;
            }
        } catch (e) {
            this._state = null;
        }
        this._render(body);
    },

    _renderTaskRow(t, current_task) {
        const obj = typeof t === 'object' ? t : {};
        const name = typeof t === 'string' ? t : (obj.name || obj.task || '?');
        const enabled = obj.enabled !== false;
        const inFlight = !!obj.in_flight;
        const lastStatus = obj.last_status;
        const isCurrent = inFlight || name === current_task;

        // Status dot reflects task health, not just enabled/disabled.
        let dotClass = 'sidebar-activity-dot dot-idle';
        if (!enabled) dotClass = 'sidebar-activity-dot dot-idle';
        else if (isCurrent) dotClass = 'sidebar-activity-dot dot-processing';
        else if (lastStatus === 'failed') dotClass = 'sidebar-status-dot dot-offline';
        else if (lastStatus === 'complete') dotClass = 'sidebar-status-dot dot-online';

        // Overdue meta: real "+13h12m" string once it's run before; a plain
        // "due" badge for never-run tasks (their overdue_str is epoch garbage).
        // A task with a currently-blocking gate (e.g. "run only when the CC
        // version changes") reads as "gated", not "overdue" — it's waiting on
        // a precondition by design, not silently falling behind (#803).
        let meta = '';
        if (enabled && !isCurrent && obj.last_gate_skip) {
            meta = `<span class="sidebar-list-item-meta" title="${_escape(obj.last_gate_skip)}">gated</span>`;
        } else if (enabled && !isCurrent && obj.overdue_by > 0) {
            meta = obj.last_run_iso
                ? `<span class="sidebar-list-item-meta overdue">${_escape(obj.overdue_str || 'overdue')}</span>`
                : `<span class="sidebar-list-item-meta">due</span>`;
        }

        const safeName = _escape(name);
        const title = _escape(`${name} — ${obj.schedule_str || ''}${obj.last_run ? ` · last: ${obj.last_run} (${lastStatus || '?'})` : ''}`);
        return `<div class="sidebar-list-item sidebar-scheduler-task" data-task="${safeName}" title="${title}">
            <span class="${dotClass}"></span>
            <span class="sidebar-list-item-title">${safeName}</span>
            ${meta}
            <button class="sidebar-list-item-btn" data-action="${enabled ? 'disable' : 'enable'}" title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '⏸' : '▶'}</button>
            <button class="sidebar-list-item-btn" data-action="run" title="Run now">⚡</button>
        </div>`;
    },

    _renderGroup(key, label, count, rowsHtml) {
        const collapsed = this._collapsed.has(key);
        const chevron = collapsed ? '▸' : '▾';
        return `<div class="sidebar-subheader-toggle" data-group="${key}" data-collapsed="${collapsed}">
            <span class="sidebar-chevron">${chevron}</span>
            <span>${label}</span>
            <span class="sidebar-subheader-count">${count}</span>
        </div>${collapsed ? '' : rowsHtml}`;
    },

    _render(body) {
        if (!this._state) {
            // Null only when the board API itself is unreachable (portal down).
            body.innerHTML = '<div class="sidebar-empty">Scheduler unavailable</div>';
            this._lastHtml = '';
            return;
        }
        const { current_task, tasks } = this._state;
        const running = this._state.running ?? (this._state.status === 'running');
        const statusDot = running ? 'dot-online' : 'dot-offline';
        const statusText = running ? 'Running' : 'Stopped';
        const toggleAction = running ? 'scheduler-stop' : 'scheduler-start';
        const toggleIcon = running ? '⏸' : '▶';
        const toggleTitle = running ? 'Stop scheduler' : 'Start scheduler';

        let html = `<div class="sidebar-list-item sidebar-scheduler-status">
            <span class="sidebar-status-dot ${statusDot}"></span>
            <span class="sidebar-list-item-title">${statusText}</span>
            <button class="sidebar-list-item-btn" data-action="${toggleAction}" title="${toggleTitle}">${toggleIcon}</button>
        </div>`;

        if (current_task) {
            html += `<div class="sidebar-section-subheader">Current</div>`;
            html += `<div class="sidebar-list-item sidebar-scheduler-current"><span class="sidebar-activity-dot dot-processing"></span><span class="sidebar-list-item-title">${_escape(current_task)}</span></div>`;
        }

        const taskList = tasks || this._state.task_list || [];
        if (taskList.length) {
            const tasksCollapsed = this._collapsed.has('tasks');
            const tasksChevron = tasksCollapsed ? '▸' : '▾';
            html += `<div class="sidebar-subheader-toggle sidebar-subheader-parent" data-group="tasks" data-collapsed="${tasksCollapsed}">
                <span class="sidebar-chevron">${tasksChevron}</span>
                <span>Tasks</span>
                <span class="sidebar-subheader-count">${taskList.length}</span>
            </div>`;

            if (!tasksCollapsed) {
                const active = [];
                const inactive = [];
                for (const t of taskList) {
                    const enabled = typeof t === 'object' ? t.enabled !== false : true;
                    (enabled ? active : inactive).push(t);
                }
                const activeRows = active.map(t => this._renderTaskRow(t, current_task)).join('');
                const inactiveRows = inactive.map(t => this._renderTaskRow(t, current_task)).join('');
                html += `<div class="sidebar-subheader-children">`;
                html += this._renderGroup('active', 'Active', active.length, activeRows);
                html += this._renderGroup('inactive', 'Inactive', inactive.length, inactiveRows);
                html += `</div>`;
            }
        }

        if (html === this._lastHtml) return;  // dedupe: WS-driven re-renders w/ same DOM
        this._lastHtml = html;
        body.innerHTML = html;
        body.onclick = (e) => this._handleClick(e, body);
    },

    async _handleClick(e, body) {
        const toggle = e.target.closest('[data-group]');
        if (toggle) {
            const key = toggle.dataset.group;
            if (this._collapsed.has(key)) this._collapsed.delete(key);
            else this._collapsed.add(key);
            _saveCollapsed(this._collapsed);
            this._render(body);
            return;
        }

        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const action = btn.dataset.action;

        // Daemon start/stop toggle (not tied to a task row).
        if (action === 'scheduler-start' || action === 'scheduler-stop') {
            const starting = action === 'scheduler-start';
            await withButtonBusy(btn, async () => {
                try {
                    const path = starting ? '/api/scheduler/start' : '/api/scheduler/stop';
                    const res = await apiFetch(path, { method: 'POST' });
                    if (!res.ok) throw new Error(await errorFromResponse(res));
                    toastSuccess(starting ? 'Scheduler started' : 'Scheduler stopped');
                    // Response-gated refresh — no fixed 1.5s guess. The daemon has
                    // acknowledged by the time the POST resolves; WS scheduler_state
                    // events keep it current after that.
                    await this.refresh(body);
                } catch (err) {
                    toastError(`Scheduler ${starting ? 'start' : 'stop'} failed: ${err.message}`);
                }
            });
            return;
        }

        const item = btn.closest('[data-task]');
        if (!item) return;
        const task = item.dataset.task;
        await withButtonBusy(btn, async () => {
            try {
                let res, msg;
                if (action === 'run') {
                    res = await apiFetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/run`, { method: 'POST' });
                    msg = `Triggered ${task}`;
                } else if (action === 'enable') {
                    res = await apiFetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/enable`, { method: 'POST' });
                    msg = `Enabled ${task}`;
                } else if (action === 'disable') {
                    res = await apiFetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/disable`, { method: 'POST' });
                    msg = `Disabled ${task}`;
                } else {
                    return;
                }
                if (!res.ok) throw new Error(await errorFromResponse(res));
                toastSuccess(msg);
                await this.refresh(body);
            } catch (err) {
                toastError(`Task ${action} failed: ${err.message}`);
            }
        });
    },
};
