/**
 * Shared rendering + state helpers for the Safety sidebar section and
 * the full Safety review WinBox window.
 */

import { apiFetch } from './api.js';

export const DECISION_LABELS = {
    blocked: 'BLOCKED',
    asked: 'ASKED',
    allowed: 'ALLOWED',
    allowed_by_escape: 'ESCAPE',
    allowed_by_disabled: 'DISABLED',
};

export const DECISION_CLASSES = {
    blocked: 'blocked',
    asked: 'asked',
    allowed: 'allowed',
    allowed_by_escape: 'escape',
    allowed_by_disabled: 'disabled-mode',
};

export function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

export function formatRel(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return '';
    const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
}

export function projectFromCwd(cwd) {
    if (!cwd) return '—';
    const parts = String(cwd).split('/').filter(Boolean);
    const idx = parts.findIndex((p) => p === 'projects');
    if (idx >= 0 && idx < parts.length - 1) return parts[idx + 1].split('-worktrees')[0];
    return parts[parts.length - 1] || '—';
}

export function eventRow(e) {
    const d = e.decision || 'allowed';
    const klass = DECISION_CLASSES[d] || 'allowed';
    const label = DECISION_LABELS[d] || d;
    const cmd = (e.command || '').slice(0, 160);
    const proj = projectFromCwd(e.cwd);
    return `<div class="safety-event" data-event='${escapeHtml(JSON.stringify(e))}'>
        <span class="safety-decision ${klass}">${label}</span>
        <span class="safety-event-cmd">${escapeHtml(cmd)}</span>
        <span class="safety-event-meta">${escapeHtml(proj)} · ${formatRel(e.timestamp)}</span>
    </div>`;
}

export async function fetchSafetyStatus() {
    try { return await apiFetch('/api/safety/status').then((r) => r.json()); } catch { return {}; }
}

export async function fetchSafetyLogs(decisionFilter = '', limit = 200) {
    const qs = `?limit=${limit}${decisionFilter ? '&decision=' + encodeURIComponent(decisionFilter) : ''}`;
    try {
        const data = await apiFetch('/api/safety/logs' + qs).then((r) => r.json());
        return data.entries || [];
    } catch { return []; }
}

export function showEventModal(e) {
    const existing = document.getElementById('safetyEventOverlay');
    if (existing) existing.remove();
    const wrap = document.createElement('div');
    wrap.id = 'safetyEventOverlay';
    wrap.className = 'modal-overlay';
    wrap.innerHTML = `<div class="modal safety-event-modal">
        <div class="modal-header">
            <h3>${escapeHtml(DECISION_LABELS[e.decision] || e.decision)}</h3>
            <button class="modal-close" data-close>×</button>
        </div>
        <div class="modal-body">
            <div class="safety-event-detail-row"><span>Timestamp</span><code>${escapeHtml(e.timestamp || '')}</code></div>
            <div class="safety-event-detail-row"><span>Tool</span><code>${escapeHtml(e.tool || '')}</code></div>
            <div class="safety-event-detail-row"><span>Session</span><code>${escapeHtml(e.session_id || 'unknown')}</code></div>
            <div class="safety-event-detail-row"><span>cwd</span><code>${escapeHtml(e.cwd || '—')}</code></div>
            ${e.rule_id ? `<div class="safety-event-detail-row"><span>Rule id</span><code>${escapeHtml(e.rule_id)}</code></div>` : ''}
            ${e.blocked_by ? `<div class="safety-event-detail-row"><span>Reason</span>${escapeHtml(e.blocked_by)}</div>` : ''}
            ${e.escape_reason ? `<div class="safety-event-detail-row"><span>Escape reason</span>${escapeHtml(e.escape_reason)}</div>` : ''}
            ${e.pattern_matched ? `<div class="safety-event-detail-row"><span>Pattern</span><code>${escapeHtml(e.pattern_matched)}</code></div>` : ''}
            <div class="safety-event-detail-cmd">
                <span>Command</span>
                <pre>${escapeHtml(e.command || '')}</pre>
            </div>
        </div>
    </div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener('click', (ev) => {
        if (ev.target === wrap || ev.target.closest('[data-close]')) wrap.remove();
    });
}
