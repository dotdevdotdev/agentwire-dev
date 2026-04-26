/**
 * sdk-sessions-section.js
 *
 * Sidebar section listing saved `agentwire repl` SDK sessions
 * (transcripts under ~/.agentwire/sessions/repl/). Each entry has a
 * "watch" button that opens a SdkWatchWindow tailing its JSONL.
 */

let allSessions = [];

async function fetchSdkSessions() {
    try {
        const res = await fetch('/api/sdk-sessions');
        const data = await res.json();
        allSessions = data.sessions || [];
    } catch (e) {
        allSessions = [];
    }
}

function renderCard(s) {
    const name = s.name || '';
    const model = s.model || '';
    const turns = s.turn_count ?? 0;
    const cost = s.total_cost_usd ?? 0;
    const isOpen = !s.ended_at;
    const dotClass = isOpen ? 'dot-processing' : 'dot-idle';
    return `<div class="sidebar-session-card" data-sdk-session="${name}">
        <div class="sidebar-session-row1">
            <span class="sidebar-activity-dot ${dotClass}"></span>
            <span class="sidebar-session-name">${name}</span>
            <button class="sidebar-list-item-btn" data-action="watch" title="Watch live">👁‍🗨</button>
        </div>
        <div class="sidebar-session-row2">
            <span class="sidebar-tag sidebar-tag-sdk">${model}</span>
            <span class="sidebar-tag">${turns} turns</span>
            ${cost ? `<span class="sidebar-tag">$${Number(cost).toFixed(4)}</span>` : ''}
        </div>
    </div>`;
}

async function handleSdkSessionClick(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const item = btn.closest('[data-sdk-session]');
    if (!item) return;
    const name = item.dataset.sdkSession;
    const action = btn.dataset.action;
    if (action === 'watch') {
        const { openSdkWatchWindow } = await import('../desktop.js');
        openSdkWatchWindow(name);
    }
}

export const sdkSessionsSection = {
    title: 'SDK sessions',
    actions: [],

    async mount(body) {
        await fetchSdkSessions();
        this._render(body);
    },

    async refresh(body) {
        await fetchSdkSessions();
        this._render(body);
    },

    _render(body) {
        if (!allSessions.length) {
            body.innerHTML = '<div class="sidebar-empty">No SDK sessions</div>';
            return;
        }
        body.innerHTML = allSessions.map(s => renderCard(s)).join('');
        body.onclick = (e) => handleSdkSessionClick(e);
    },
};
