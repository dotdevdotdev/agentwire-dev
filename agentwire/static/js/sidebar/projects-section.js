export const projectsSection = {
    title: 'Projects',
    autoRefreshMs: 30000,
    _body: null,

    async mount(body) {
        this._body = body;
        await this.refresh(body);
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
            const { openQuicktaskModal } = await import('../quicktask-modal.js');
            openQuicktaskModal({ project: name });
            return;
        }

        if (action === 'open' && path) {
            try {
                const res = await fetch('/api/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path, machine }),
                });
                if (res.ok) {
                    const data = await res.json();
                    const { openSessionTerminal } = await import('../desktop.js');
                    openSessionTerminal(data.session || data.name, 'terminal', machine);
                }
            } catch (e) { console.warn('Failed to create session from project', e); }
        }
    },
};
