import { desktop } from '../desktop-manager.js';

export const schedulerSection = {
    title: 'Scheduler',
    _body: null,
    _state: null,

    async mount(body) {
        this._body = body;

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
        try {
            const [liveRes, boardRes] = await Promise.all([
                fetch('/api/scheduler/live'),
                fetch('/api/scheduler/board'),
            ]);
            const live = liveRes.ok ? await liveRes.json() : null;
            const board = boardRes.ok ? await boardRes.json() : null;
            if (live) {
                this._state = { ...live, tasks: board?.tasks || [] };
            } else {
                this._state = null;
            }
        } catch (e) {
            this._state = null;
        }
        this._render(body);
    },

    _render(body) {
        if (!this._state) {
            body.innerHTML = '<div class="sidebar-empty">Scheduler not running</div>';
            return;
        }
        const { current_task, tasks, uptime } = this._state;
        // Tolerate both shapes: HTTP /live sends `running: true`; older WS
        // events only had `status: "running"`.
        const running = this._state.running ?? (this._state.status === 'running');
        const statusDot = running ? 'dot-online' : 'dot-offline';
        const statusText = running ? 'Running' : 'Stopped';

        let html = `<div class="sidebar-list-item"><span class="sidebar-status-dot ${statusDot}"></span><span class="sidebar-list-item-title">${statusText}</span></div>`;

        if (current_task) {
            html += `<div class="sidebar-section-subheader">Current</div>`;
            html += `<div class="sidebar-list-item sidebar-scheduler-current"><span class="sidebar-activity-dot dot-processing"></span><span class="sidebar-list-item-title">${current_task}</span></div>`;
        }

        const taskList = tasks || this._state.task_list || [];
        if (taskList.length) {
            html += `<div class="sidebar-section-subheader">Tasks</div>`;
            for (const t of taskList) {
                const name = typeof t === 'string' ? t : (t.name || t.task || '?');
                const enabled = typeof t === 'object' ? t.enabled !== false : true;
                const statusClass = !enabled ? 'dot-offline' : (name === current_task ? 'dot-processing' : 'dot-idle');
                html += `<div class="sidebar-list-item sidebar-scheduler-task" data-task="${name}">
                    <span class="sidebar-status-dot ${statusClass}"></span>
                    <span class="sidebar-list-item-title">${name}</span>
                    <button class="sidebar-list-item-btn" data-action="${enabled ? 'disable' : 'enable'}" title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '⏸' : '▶'}</button>
                    <button class="sidebar-list-item-btn" data-action="run" title="Run now">⚡</button>
                </div>`;
            }
        }

        body.innerHTML = html;
        body.onclick = (e) => this._handleClick(e, body);
    },

    async _handleClick(e, body) {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const item = btn.closest('[data-task]');
        if (!item) return;
        const task = item.dataset.task;
        const action = btn.dataset.action;
        try {
            if (action === 'run') {
                await fetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/run`, { method: 'POST' });
            } else if (action === 'enable') {
                await fetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/enable`, { method: 'POST' });
            } else if (action === 'disable') {
                await fetch(`/api/scheduler/tasks/${encodeURIComponent(task)}/disable`, { method: 'POST' });
            }
            await this.refresh(body);
        } catch (e) { console.warn('Scheduler action failed', e); }
    },
};
