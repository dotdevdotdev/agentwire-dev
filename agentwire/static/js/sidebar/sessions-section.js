import { desktop } from '../desktop-manager.js';

// Shared state across sessions and services sections
export const activityStates = new Map();
export function isService(name) { return name.startsWith('agentwire-'); }

const SOCIAL_PREFIXES = [
    'discord-dm-', 'slack-dm-', 'telegram-dm-',
    'discord-ch-', 'slack-ch-',
];
const SOCIAL_ROLES = ['discord-dm', 'slack-dm', 'telegram-dm'];

export function isSocial(session) {
    const name = session.name || '';
    if (SOCIAL_PREFIXES.some(p => name.startsWith(p))) return true;
    const roles = session.roles || [];
    return roles.some(r => SOCIAL_ROLES.includes(r));
}

let allSessions = [];
const listeners = new Set();

export function getAllSessions() { return allSessions; }
export function onSessionsChanged(fn) { listeners.add(fn); }

function notifyListeners() { for (const fn of listeners) fn(); }

export function renderCard(s) {
    const name = s.name || '';
    const machine = s.machine || null;
    const id = machine ? `${name}@${machine}` : name;
    const activity = activityStates.get(name) || s.activity || 'idle';
    const dotClass = activity === 'idle' ? 'dot-idle' : activity === 'processing' ? 'dot-processing' : activity === 'generating' ? 'dot-generating' : 'dot-playing';
    const tags = [];
    if (s.type) {
        const typeClass = String(s.type).startsWith('sdk-') ? 'sidebar-tag sidebar-tag-sdk' : 'sidebar-tag';
        tags.push(`<span class="${typeClass}">${s.type}</span>`);
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
    const machine = item.dataset.machine || null;
    const action = btn.dataset.action;
    const { openSessionTerminal } = await import('../desktop.js');
    if (action === 'connect') openSessionTerminal(session, 'terminal', machine);
    else if (action === 'monitor') openSessionTerminal(session, 'monitor', machine);
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
        const localRes = await fetch('/api/sessions/local');
        const localData = await localRes.json();
        allSessions = localData.sessions || [];
        notifyListeners();
    } catch (e) {
        allSessions = [];
        notifyListeners();
    }
    fetch('/api/sessions/remote').then(async (res) => {
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
                const res = await fetch('/api/create', {
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
        const work = allSessions.filter(s => !isService(s.name || '') && !isSocial(s));
        let html = this._renderForm();
        if (!work.length && !this._formType) {
            html += '<div class="sidebar-empty">No sessions</div>';
        } else {
            html += work.map(s => renderCard(s)).join('');
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
