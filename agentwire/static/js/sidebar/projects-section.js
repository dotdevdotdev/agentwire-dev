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
                        <button class="sidebar-list-item-btn" data-action="worktree" title="New worktree session">⎇</button>
                        <button class="sidebar-list-item-btn" data-action="open" title="Open session">▸</button>
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

        if (action === 'open' && name) {
            const [{ openSessionTerminal }, { sidebar }] = await Promise.all([
                import('../desktop.js'),
                import('../sidebar.js'),
            ]);
            try {
                const res = await fetch('/api/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, path, machine }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || data.error) {
                    console.warn('Failed to create session from project:', data.error || `HTTP ${res.status}`);
                    return;
                }
                sidebar.close();
                openSessionTerminal(data.session || data.name || name, 'terminal', machine);
            } catch (e) {
                console.warn('Failed to create session from project', e);
            }
        }
    },
};
