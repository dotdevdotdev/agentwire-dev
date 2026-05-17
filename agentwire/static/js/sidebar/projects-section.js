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
        const [{ openNewProjectModal }, { sidebar }] = await Promise.all([
            import('../new-project-modal.js'),
            import('../sidebar.js'),
        ]);
        sidebar.close();
        openNewProjectModal({
            onCreated: () => { this.refresh(body); },
        });
    },

    async refresh(body) {
        try {
            const res = await fetch('/api/projects');
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
        const machine = item.dataset.machine || null;
        const name = item.dataset.name || '';

        if (action === 'worktree' && name) {
            const [{ openQuicktaskModal }, { sidebar }] = await Promise.all([
                import('../quicktask-modal.js'),
                import('../sidebar.js'),
            ]);
            sidebar.close();
            openQuicktaskModal({ project: name });
            return;
        }

        if (action === 'start' && name) {
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
                const url = machine && machine !== 'local'
                    ? `/api/sessions/remote?machine=${encodeURIComponent(machine)}`
                    : '/api/sessions/local';
                const r = await fetch(url);
                const d = await r.json().catch(() => ({}));
                const sessions = d.sessions
                    || (d.machines || []).flatMap((m) => m.sessions || []);
                const target = (machine && machine !== 'local') ? machine : null;
                if (sessions.some((s) => s.name === name && (s.machine || null) === target)) {
                    open(name);
                    return;
                }
            } catch (e) { /* fall through and try to create */ }

            // Create fresh session
            try {
                const res = await fetch('/api/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, path, machine }),
                });
                const data = await res.json().catch(() => ({}));
                const err = data.error || '';
                // If a race made the session appear, that's fine — just open it.
                if (!res.ok || (err && !/already exists/i.test(err))) {
                    console.warn('Failed to start session from project:', err || `HTTP ${res.status}`);
                    return;
                }
                open(data.session || data.name || name);
            } catch (e) {
                console.warn('Failed to start session from project', e);
            }
        }
    },
};
