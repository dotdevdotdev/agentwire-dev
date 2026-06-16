import { apiFetch } from '../api.js';
import { normalizeMachine, sameMachine } from '../session-id.js';
import { toastSuccess, toastError, withButtonBusy } from '../toast.js';

export const projectsSection = {
    title: 'Projects',
    autoRefreshMs: 30000,
    actions: [{ id: 'new', label: '+', title: 'New project' }],
    _body: null,

    async mount(body) {
        this._body = body;
        await this.refresh(body);
    },

    async onAction(actionId, body) {
        if (actionId !== 'new') return;
        const [{ openCommandPalette }, { sidebar }] = await Promise.all([
            import('../command-palette.js'),
            import('../sidebar.js'),
        ]);
        sidebar.close();
        // Idea-first capture — the palette flow creates the project, spawns
        // the session (delivering the idea as the first message), and opens
        // the window itself.
        openCommandPalette({ view: 'new-idea' });
    },

    async refresh(body) {
        try {
            const res = await apiFetch('/api/projects');
            const data = await res.json();
            const projects = data.projects || [];
            if (!projects.length) {
                body.innerHTML = '<div class="sidebar-empty">No projects</div>';
                return;
            }
            // Group by machine
            const groups = {};
            for (const p of projects) {
                const key = p.machine || 'local';
                (groups[key] ||= []).push(p);
            }
            let html = '';
            for (const [machine, items] of Object.entries(groups)) {
                if (Object.keys(groups).length > 1) {
                    html += `<div class="sidebar-section-subheader">${machine}</div>`;
                }
                for (const p of items) {
                    const name = p.name || p.path?.split('/').pop() || '?';
                    html += `<div class="sidebar-list-item sidebar-project-item" data-path="${p.path || ''}" data-machine="${p.machine || ''}" data-name="${name}">
                        <span class="sidebar-list-item-title">${name}</span>
                        <button class="sidebar-list-item-btn" data-action="worktree" title="New worktree session for this project">⎇</button>
                        <button class="sidebar-list-item-btn" data-action="start" title="Start session for this project (resumes if already running)">▶</button>
                    </div>`;
                }
            }
            body.innerHTML = html;
        } catch (e) {
            body.innerHTML = '<div class="sidebar-empty">Failed to load projects</div>';
        }
        body.onclick = (e) => this._handleClick(e, body);
    },

    async _handleClick(e, body) {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const item = btn.closest('[data-path]');
        if (!item) return;
        const action = btn.dataset.action;
        const path = item.dataset.path;
        const machine = normalizeMachine(item.dataset.machine);
        const name = item.dataset.name || '';

        if (action === 'worktree' && name) {
            const [{ openCommandPalette }, { sidebar }] = await Promise.all([
                import('../command-palette.js'),
                import('../sidebar.js'),
            ]);
            sidebar.close();
            openCommandPalette({ view: 'worktree', project: name });
            return;
        }

        if (action === 'start' && name) {
            await withButtonBusy(btn, () => this._startProjectSession({ name, path, machine }));
        }
    },

    /**
     * Resume the project's session if one already exists, otherwise create it.
     * Either way, close the sidebar and attach the terminal window.
     */
    async _startProjectSession({ name, path, machine }) {
        if (!name) return;
        const [{ openSessionTerminal }, { sidebar }] = await Promise.all([
            import('../desktop.js'),
            import('../sidebar.js'),
        ]);
        const open = (sessionName) => {
            sidebar.close();
            openSessionTerminal(sessionName, 'terminal', machine);
        };

        // Fast path: if a session with this name already exists, just resume it.
        try {
            const url = machine
                ? `/api/sessions/remote?machine=${encodeURIComponent(machine)}`
                : '/api/sessions/local';
            const r = await apiFetch(url);
            const d = await r.json().catch(() => ({}));
            const sessions = d.sessions
                || (d.machines || []).flatMap((m) => m.sessions || []);
            if (sessions.some((s) => s.name === name && sameMachine(s.machine, machine))) {
                open(name);
                return;
            }
        } catch (e) { /* fall through and try to create */ }

        // Create fresh session
        try {
            const res = await apiFetch('/api/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, path, machine }),
            });
            const data = await res.json().catch(() => ({}));
            const err = data.error || '';
            // If a race made the session appear, that's fine — just open it.
            if (!res.ok || (err && !/already exists/i.test(err))) {
                toastError(`Failed to start ${name}: ${err || `HTTP ${res.status}`}`);
                return;
            }
            toastSuccess(`Started ${name}`);
            open(data.session || data.name || name);
        } catch (e) {
            toastError(`Failed to start ${name}: ${e.message}`);
        }
    },
};
