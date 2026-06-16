
import { apiFetch } from '../api.js';
import { toastSuccess, toastError, withButtonBusy, errorFromResponse } from '../toast.js';
export const machinesSection = {
    title: 'Machines',
    autoRefreshMs: 10000,
    async mount(body) { await this.refresh(body); },
    async refresh(body) {
        try {
            const res = await apiFetch('/api/machines');
            const data = await res.json();
            const machines = Array.isArray(data) ? data : (data.machines || []);
            if (!machines.length) {
                body.innerHTML = '<div class="sidebar-empty">No machines</div>';
                return;
            }
            body.innerHTML = machines.map(m => {
                const status = m.status || 'unknown';
                const dotClass = status === 'online' ? 'dot-online' : status === 'checking' ? 'dot-checking' : 'dot-offline';
                const label = m.host || m.id;
                const tag = m.id === 'local' ? '<span class="sidebar-tag">local</span>' : '';
                return `<div class="sidebar-list-item sidebar-machine-item" data-machine="${m.id}">
                    <span class="sidebar-status-dot ${dotClass}"></span>
                    <span class="sidebar-list-item-title">${label}</span>
                    ${tag}
                    <button class="sidebar-list-item-btn" data-action="new-session" title="New Session">+</button>
                </div>`;
            }).join('');
        } catch (e) {
            body.innerHTML = '<div class="sidebar-empty">Failed to load machines</div>';
        }
        body.onclick = (e) => this._handleClick(e, body);
    },
    async _handleClick(e, body) {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const item = btn.closest('[data-machine]');
        if (!item) return;
        const machineId = item.dataset.machine;
        if (btn.dataset.action === 'new-session') {
            await withButtonBusy(btn, async () => {
                try {
                    const machine = machineId === 'local' ? null : machineId;
                    const res = await apiFetch('/api/create', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ machine }),
                    });
                    if (!res.ok) throw new Error(await errorFromResponse(res));
                    const data = await res.json();
                    const sessionName = data.session || data.name;
                    toastSuccess(`Created ${sessionName}`);
                    const { openSessionTerminal } = await import('../desktop.js');
                    openSessionTerminal(sessionName, 'terminal', machine);
                } catch (err) {
                    toastError(`Failed to create session: ${err.message}`);
                }
            });
        }
    },
};
