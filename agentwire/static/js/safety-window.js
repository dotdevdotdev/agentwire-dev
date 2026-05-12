/**
 * Safety review WinBox window — full admin view of damage-control history,
 * disabled rules, and the master toggle. Opened from the sidebar's "Show all"
 * button.
 */

import {
    eventRow,
    escapeHtml,
    fetchSafetyStatus,
    fetchSafetyLogs,
    postSafetyConfig,
    showEventModal,
    openAddRulePicker,
} from './safety-shared.js';

let activeWindow = null;
const state = {
    decision: '',
    project: '',
    limit: 500,
};

function pageHtml(status, entries) {
    const enabled = status?.enabled ?? true;
    const counts = status?.today_counts || {};
    const fmt = (k) => counts[k] || 0;
    const disabled = status?.disabled_rules || [];
    const projectOptions = Array.from(new Set(entries.map((e) => projectFromCwd(e.cwd)))).filter(Boolean);
    return `
        <div class="safety-window-content">
            <aside class="safety-window-sidebar">
                <label class="safety-toggle">
                    <input type="checkbox" data-action="toggle-enabled" ${enabled ? 'checked' : ''} />
                    <span>Damage control ${enabled ? 'enabled' : 'disabled'}</span>
                </label>
                <div class="safety-window-stats">
                    <div class="safety-window-stat"><span class="safety-decision blocked">${fmt('blocked')}</span><label>blocked</label></div>
                    <div class="safety-window-stat"><span class="safety-decision escape">${fmt('allowed_by_escape')}</span><label>escape</label></div>
                    <div class="safety-window-stat"><span class="safety-decision asked">${fmt('asked')}</span><label>asked</label></div>
                    <div class="safety-window-stat"><span class="safety-decision allowed">${fmt('allowed')}</span><label>allowed</label></div>
                    <div class="safety-window-stat"><span class="safety-decision disabled-mode">${fmt('allowed_by_disabled')}</span><label>disabled</label></div>
                </div>

                <div class="safety-window-section">
                    <h4>Filters</h4>
                    <div class="safety-filters">
                        <button class="safety-chip ${state.decision === '' ? 'active' : ''}" data-decision="">all</button>
                        <button class="safety-chip ${state.decision === 'blocked' ? 'active' : ''}" data-decision="blocked">blocked</button>
                        <button class="safety-chip ${state.decision === 'allowed_by_escape' ? 'active' : ''}" data-decision="allowed_by_escape">escape</button>
                        <button class="safety-chip ${state.decision === 'asked' ? 'active' : ''}" data-decision="asked">asked</button>
                        <button class="safety-chip ${state.decision === 'allowed_by_disabled' ? 'active' : ''}" data-decision="allowed_by_disabled">disabled</button>
                    </div>
                    <select class="safety-project-select" data-action="filter-project">
                        <option value="">All projects</option>
                        ${projectOptions.map((p) => `<option value="${escapeHtml(p)}" ${state.project === p ? 'selected' : ''}>${escapeHtml(p)}</option>`).join('')}
                    </select>
                </div>

                <div class="safety-window-section">
                    <div class="safety-disabled-rules-header">
                        <h4>Disabled rules (${disabled.length})</h4>
                        <button class="safety-disabled-add" data-action="add-rule">+</button>
                    </div>
                    <div class="safety-disabled-rules-list">
                        ${disabled.length ? disabled.map((id) => `
                            <div class="safety-disabled-rule" data-rule-id="${escapeHtml(id)}">
                                <code>${escapeHtml(id)}</code>
                                <button class="safety-rule-remove" data-action="remove-rule" data-rule-id="${escapeHtml(id)}" title="Re-enable rule">×</button>
                            </div>
                        `).join('') : '<div class="safety-empty">No rules disabled.</div>'}
                    </div>
                </div>

                <button class="safety-refresh-btn" data-action="refresh">↻ Refresh</button>
            </aside>

            <main class="safety-window-main">
                <div class="safety-window-events">
                    ${entries.length
                        ? entries.map(eventRow).join('')
                        : '<div class="sidebar-empty">No events match the current filter.</div>'}
                </div>
            </main>
        </div>
    `;
}

function projectFromCwd(cwd) {
    if (!cwd) return '';
    const parts = String(cwd).split('/').filter(Boolean);
    const idx = parts.findIndex((p) => p === 'projects');
    if (idx >= 0 && idx < parts.length - 1) return parts[idx + 1].split('-worktrees')[0];
    return parts[parts.length - 1] || '';
}

async function loadAndRender(container) {
    container.innerHTML = '<div class="sidebar-empty">Loading…</div>';
    const [status, allEntries] = await Promise.all([
        fetchSafetyStatus(),
        fetchSafetyLogs(state.decision, state.limit),
    ]);
    let entries = allEntries.slice().reverse();
    if (state.project) {
        entries = entries.filter((e) => projectFromCwd(e.cwd) === state.project);
    }
    container.innerHTML = pageHtml(status, entries);
    bindControls(container, status);
}

function bindControls(container, status) {
    container.querySelector('[data-action="toggle-enabled"]')?.addEventListener('change', async (e) => {
        await postSafetyConfig({ enabled: e.target.checked });
        loadAndRender(container);
    });
    container.querySelectorAll('.safety-chip').forEach((chip) => {
        chip.addEventListener('click', () => {
            state.decision = chip.dataset.decision || '';
            loadAndRender(container);
        });
    });
    container.querySelector('[data-action="filter-project"]')?.addEventListener('change', (e) => {
        state.project = e.target.value || '';
        loadAndRender(container);
    });
    container.querySelectorAll('[data-action="remove-rule"]').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = btn.dataset.ruleId;
            const next = (status.disabled_rules || []).filter((r) => r !== id);
            await postSafetyConfig({ disabled_rules: next });
            loadAndRender(container);
        });
    });
    container.querySelector('[data-action="add-rule"]')?.addEventListener('click', () => {
        openAddRulePicker(status.disabled_rules || [], () => loadAndRender(container));
    });
    container.querySelector('[data-action="refresh"]')?.addEventListener('click', () => loadAndRender(container));
    container.querySelectorAll('.safety-event').forEach((row) => {
        row.addEventListener('click', () => {
            try {
                showEventModal(JSON.parse(row.dataset.event), () => loadAndRender(container));
            } catch (_) {}
        });
    });
}

export function openSafetyWindow() {
    if (activeWindow && activeWindow.window) {
        try { activeWindow.focus(); return; } catch (_) {}
    }
    const container = document.createElement('div');
    container.className = 'safety-window-mount';
    activeWindow = new WinBox({
        title: 'Safety review',
        icon: '<span style="font-size:14px">🛡️</span>',
        mount: container,
        width: '100%',
        height: '100%',
        x: 0,
        y: 0,
        minwidth: 640,
        minheight: 420,
        class: ['safety-window'],
        onclose: () => {
            activeWindow = null;
            return false;
        },
    });
    try { activeWindow.maximize(); } catch (_) {}
    loadAndRender(container);
}
