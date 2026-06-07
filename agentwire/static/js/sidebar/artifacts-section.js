
import { apiFetch } from '../api.js';
export const artifactsSection = {
    title: 'Artifacts',
    async mount(body) { await this.refresh(body); },
    async refresh(body) {
        try {
            const res = await apiFetch('/api/artifacts');
            const items = await res.json();
            if (!items.length) {
                body.innerHTML = '<div class="sidebar-empty">No artifacts</div>';
                return;
            }
            body.innerHTML = items.map(a => {
                const size = a.size < 1024 ? `${a.size}B` : `${(a.size / 1024).toFixed(1)}K`;
                return `<div class="sidebar-list-item" data-name="${a.name}">
                    <span class="sidebar-list-item-title">${a.name}</span>
                    <span class="sidebar-list-item-meta">${size}</span>
                    <button class="sidebar-list-item-btn" data-action="open" title="Open">↗</button>
                    <button class="sidebar-list-item-btn sidebar-list-item-btn-danger" data-action="delete" title="Delete">×</button>
                </div>`;
            }).join('');
            body.addEventListener('click', (e) => this._handleClick(e, body), { once: false });
        } catch (e) {
            body.innerHTML = '<div class="sidebar-empty">Failed to load artifacts</div>';
        }
    },
    async _handleClick(e, body) {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const item = btn.closest('[data-name]');
        if (!item) return;
        const name = item.dataset.name;
        if (btn.dataset.action === 'open') {
            const { openArtifactWindow } = await import('../desktop.js');
            openArtifactWindow(name, name);
        } else if (btn.dataset.action === 'delete') {
            try {
                await apiFetch(`/api/artifacts/${encodeURIComponent(name)}`, { method: 'DELETE' });
                await this.refresh(body);
            } catch (e) { console.warn('Failed to delete artifact', e); }
        }
    },
};
