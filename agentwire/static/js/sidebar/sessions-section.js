import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';
import { buildSessionId, normalizeMachine } from '../session-id.js';

// Shared state across sessions and services sections
export const activityStates = new Map();

const SERVICE_SESSIONS = new Set([
    'agentwire-portal',
    'agentwire-tts',
    'agentwire-stt',
    'agentwire-scheduler',
    'agentwire-notifications',
]);
export function isService(name) { return SERVICE_SESSIONS.has(name); }

// Merge config-defined custom services into the Services column. Fire-and-forget
// on load; re-render once they arrive so flagged sessions hop to the right group.
async function loadCustomServices() {
    try {
        const res = await apiFetch('/api/services/custom');
        if (!res.ok) return;
        const { names } = await res.json();
        let changed = false;
        for (const n of names || []) {
            if (!SERVICE_SESSIONS.has(n)) { SERVICE_SESSIONS.add(n); changed = true; }
        }
        if (changed) notifyListeners();
    } catch {
        // Portal offline / endpoint missing — built-in services still group fine.
    }
}
loadCustomServices();

let allSessions = [];
const listeners = new Set();

// Close-button state: session name → confirm-expiry timer / in-flight kill.
// Lives at module level so it survives the frequent activity re-renders.
const pendingClose = new Map();
const killingSessions = new Set();

export function getAllSessions() { return allSessions; }
export function onSessionsChanged(fn) { listeners.add(fn); }

function notifyListeners() { for (const fn of listeners) fn(); }

function renderCloseButton(name) {
    if (killingSessions.has(name)) {
        return '<button class="sidebar-list-item-btn sidebar-session-close is-killing" data-action="close" title="Shutting down…" disabled>…</button>';
    }
    if (pendingClose.has(name)) {
        return '<button class="sidebar-list-item-btn sidebar-session-close is-confirm" data-action="close" title="Click again to kill the session">sure?</button>';
    }
    return '<button class="sidebar-list-item-btn sidebar-list-item-btn-danger sidebar-session-close" data-action="close" title="Kill session (graceful /exit, then tmux kill)">✕</button>';
}

export function renderCard(s, opts = {}) {
    const name = s.name || '';
    const machine = normalizeMachine(s.machine);
    const id = buildSessionId(name, machine);
    const activity = activityStates.get(name) || s.activity || 'idle';
    const dotClass = activity === 'idle' ? 'dot-idle' : activity === 'processing' ? 'dot-processing' : activity === 'generating' ? 'dot-generating' : 'dot-playing';
    const tags = [];
    if (s.type) {
        tags.push(`<span class="sidebar-tag">${s.type}</span>`);
    }
    if (machine) tags.push(`<span class="sidebar-tag">@${machine}</span>`);
    const roles = (s.roles || []).map(r => `<span class="sidebar-tag sidebar-tag-role">${r}</span>`).join('');
    const path = s.path ? s.path.replace(/^\/Users\/[^/]+\//, '~/') : '';
    const tagsHtml = `${tags.join('')}${roles}`;
    return `<div class="sidebar-session-card" data-session="${name}" data-machine="${machine || ''}" data-id="${id}">
        <div class="sidebar-session-row1">
            <span class="sidebar-activity-dot ${dotClass}" data-session-dot="${name}"></span>
            <span class="sidebar-session-name">${name}</span>
            <button class="sidebar-list-item-btn" data-action="connect" title="Connect">▸</button>
            <button class="sidebar-list-item-btn" data-action="monitor" title="Monitor">👁</button>
            ${opts.closable ? renderCloseButton(name) : ''}
        </div>
        ${path ? `<div class="sidebar-session-row2"><span class="sidebar-session-path">${path}</span></div>` : ''}
        ${tagsHtml ? `<div class="sidebar-session-row3">${tagsHtml}</div>` : ''}
    </div>`;
}

export function updateActivityDot(body, session) {
    const dot = body.querySelector(`[data-session-dot="${CSS.escape(session)}"]`);
    if (!dot) return;
    dot.className = 'sidebar-activity-dot';
    const state = activityStates.get(session) || 'idle';
    dot.classList.add(state === 'idle' ? 'dot-idle' : state === 'processing' ? 'dot-processing' : state === 'generating' ? 'dot-generating' : 'dot-playing');
}

export async function handleSessionClick(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const item = btn.closest('[data-session]');
    if (!item) return;
    const session = item.dataset.session;
    const machine = normalizeMachine(item.dataset.machine);
    const action = btn.dataset.action;
    if (action === 'close') {
        handleCloseClick(session);
        return;
    }
    const { openSessionTerminal } = await import('../desktop.js');
    if (action === 'connect') openSessionTerminal(session, 'terminal', machine);
    else if (action === 'monitor') openSessionTerminal(session, 'monitor', machine);
}

// First click arms an inline "sure?" confirm (auto-reverts after 3s);
// second click does the real teardown: graceful /exit + tmux kill via
// DELETE /api/sessions/{name} (thin wrapper over `agentwire kill`).
async function handleCloseClick(session) {
    if (killingSessions.has(session)) return;
    const timer = pendingClose.get(session);
    if (timer === undefined) {
        pendingClose.set(session, setTimeout(() => {
            pendingClose.delete(session);
            notifyListeners();
        }, 3000));
        notifyListeners();
        return;
    }
    clearTimeout(timer);
    pendingClose.delete(session);
    killingSessions.add(session);
    notifyListeners();
    try {
        const res = await apiFetch(`/api/sessions/${encodeURIComponent(session)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        // The portal broadcasts sessions_update after the kill; drop the row
        // locally too so the sidebar doesn't wait on the round-trip.
        allSessions = allSessions.filter(s => (s.name || '') !== session);
    } catch (err) {
        console.error(`Failed to kill session ${session}:`, err);
    } finally {
        killingSessions.delete(session);
        notifyListeners();
    }
}

// Data fetching + WebSocket events (registered once by sessionsSection)
let dataInitialized = false;

function initData() {
    if (dataInitialized) return;
    dataInitialized = true;

    desktop.on('sessions', (sessions) => {
        allSessions = sessions;
        notifyListeners();
    });
    desktop.on('session_activity', ({ session, active }) => {
        const prev = activityStates.get(session);
        if (prev === 'generating' || prev === 'playing') return;
        activityStates.set(session, active ? 'processing' : 'idle');
        notifyListeners();
    });
    desktop.on('tts_start', ({ session }) => {
        activityStates.set(session, 'generating');
        notifyListeners();
    });
    desktop.on('audio', ({ session }) => {
        activityStates.set(session, 'playing');
        notifyListeners();
    });
    desktop.on('audio_ended', ({ session }) => {
        activityStates.set(session, 'idle');
        notifyListeners();
    });
}

async function fetchSessions() {
    try {
        const localRes = await apiFetch('/api/sessions/local');
        const localData = await localRes.json();
        allSessions = localData.sessions || [];
        notifyListeners();
    } catch (e) {
        allSessions = [];
        notifyListeners();
    }
    apiFetch('/api/sessions/remote').then(async (res) => {
        try {
            const data = await res.json();
            const remote = data.sessions || [];
            if (remote.length) {
                const localNames = new Set(allSessions.map(s => s.name));
                for (const s of remote) {
                    if (!localNames.has(s.name)) allSessions.push(s);
                }
                notifyListeners();
            }
        } catch (e) {}
    }).catch(() => {});
}

export const sessionsSection = {
    title: 'Sessions',
    actions: [
        { id: 'new', label: '+', title: 'New session' },
        { id: 'worktree', label: '⎇', title: 'New worktree session' },
    ],
    _body: null,
    _formType: null,  // null | 'new' | 'worktree'

    async mount(body) {
        this._body = body;
        initData();
        onSessionsChanged(() => this._render(body));
        await fetchSessions();
    },

    async refresh(body) {
        await fetchSessions();
    },

    onAction(actionId, body) {
        if (this._formType === actionId) {
            this._formType = null;
        } else {
            this._formType = actionId;
        }
        this._render(body);
        const input = body.querySelector('.sidebar-form input[name="name"], .sidebar-form input[name="path"]');
        input?.focus();
    },

    _renderForm() {
        if (!this._formType) return '';
        const isWorktree = this._formType === 'worktree';
        return `<div class="sidebar-form">
            ${isWorktree ? '' : '<input type="text" name="name" placeholder="Session name" autocomplete="off" />'}
            <input type="text" name="path" placeholder="Path (e.g. ~/projects/foo)" autocomplete="off" />
            ${isWorktree ? '<input type="text" name="branch" placeholder="Branch name" autocomplete="off" />' : ''}
            ${isWorktree ? '<input type="text" name="base" placeholder="Base branch (default: main)" autocomplete="off" />' : ''}
            <div class="sidebar-form-row">
                <button class="sidebar-form-btn" data-form-action="submit">${isWorktree ? 'Create worktree' : 'Create'}</button>
                <button class="sidebar-form-btn sidebar-form-btn-cancel" data-form-action="cancel">Cancel</button>
            </div>
        </div>`;
    },

    async _handleFormClick(e, body) {
        const btn = e.target.closest('[data-form-action]');
        if (!btn) return;
        const action = btn.dataset.formAction;
        if (action === 'cancel') {
            this._formType = null;
            this._render(body);
            return;
        }
        if (action === 'submit') {
            const form = body.querySelector('.sidebar-form');
            const isWorktree = this._formType === 'worktree';
            const path = form.querySelector('input[name="path"]')?.value.trim();
            let name;
            if (isWorktree) {
                if (!path) return;
                // Derive project name from path basename
                name = path.replace(/\/+$/, '').split('/').pop().replace(/^~/, '');
                if (!name) return;
            } else {
                name = form.querySelector('input[name="name"]')?.value.trim();
                if (!name) return;
            }
            const branch = isWorktree ? (form.querySelector('input[name="branch"]')?.value.trim() || '') : '';
            if (isWorktree && !branch) return;
            btn.disabled = true;
            btn.textContent = 'Creating...';
            try {
                const payload = { name };
                if (path) payload.path = path;
                if (isWorktree) {
                    payload.worktree = true;
                    payload.branch = branch;
                }
                const res = await apiFetch('/api/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (res.ok) {
                    const data = await res.json();
                    this._formType = null;
                    this._render(body);
                    const { openSessionTerminal } = await import('../desktop.js');
                    const sessionName = data.session || data.name || name;
                    openSessionTerminal(sessionName, 'terminal');
                } else {
                    const err = await res.json().catch(() => ({}));
                    btn.textContent = err.error || 'Error';
                    setTimeout(() => { btn.disabled = false; btn.textContent = isWorktree ? 'Create worktree' : 'Create'; }, 2000);
                }
            } catch (e) {
                btn.textContent = 'Error';
                setTimeout(() => { btn.disabled = false; btn.textContent = isWorktree ? 'Create worktree' : 'Create'; }, 2000);
            }
        }
    },

    _render(body) {
        const work = allSessions.filter(s => !isService(s.name || ''));
        let html = this._renderForm();
        if (!work.length && !this._formType) {
            html += '<div class="sidebar-empty">No sessions</div>';
        } else {
            html += work.map(s => renderCard(s, { closable: true })).join('');
        }
        body.innerHTML = html;
        body.onclick = (e) => {
            if (e.target.closest('.sidebar-form')) {
                this._handleFormClick(e, body);
                return;
            }
            handleSessionClick(e);
        };
        // Enter key submits form
        body.querySelector('.sidebar-form')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                body.querySelector('[data-form-action="submit"]')?.click();
            }
        });
    },
};
