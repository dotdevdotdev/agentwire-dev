
import { apiFetch } from '../api.js';
import { toastSuccess, toastError, withButtonBusy, errorFromResponse } from '../toast.js';
export const artifactsSection = {
    title: 'Artifacts',
    async mount(body) { await this.refresh(body); },
    async refresh(body) {
        try {
            const res = await apiFetch('/api/artifacts');
            const items = await res.json();
            if (!items.length) {
                body.innerHTML = '<div class="sidebar-empty">No artifacts yet</div>';
            } else {
                body.innerHTML = items.map(a => {
                    const size = a.size < 1024 ? `${a.size}B` : `${(a.size / 1024).toFixed(1)}K`;
                    return `<div class="sidebar-list-item" data-name="${a.name}">
                        <span class="sidebar-list-item-title">${a.name}</span>
                        <span class="sidebar-list-item-meta">${size}</span>
                        <button class="sidebar-list-item-btn" data-action="open" title="Open">↗</button>
                        <button class="sidebar-list-item-btn sidebar-list-item-btn-danger" data-action="delete" title="Delete">×</button>
                    </div>`;
                }).join('');
            }
        } catch (e) {
            body.innerHTML = '<div class="sidebar-empty">Failed to load artifacts</div>';
        }
        // Idempotent assignment = single delegated handler. The old
        // addEventListener-every-refresh stacked listeners, so each click fired
        // N times (issue #17 double-firing handler).
        body.onclick = (e) => this._handleClick(e, body);
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
            await withButtonBusy(btn, async () => {
                try {
                    const res = await apiFetch(`/api/artifacts/${encodeURIComponent(name)}`, { method: 'DELETE' });
                    if (!res.ok) throw new Error(await errorFromResponse(res));
                    toastSuccess(`Deleted ${name}`);
                    await this.refresh(body);
                } catch (err) {
                    toastError(`Failed to delete ${name}: ${err.message}`);
                }
            });
        }
    },
};
