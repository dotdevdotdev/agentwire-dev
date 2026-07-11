import { apiFetch } from '../api.js';
import { desktop } from '../desktop-manager.js';
import { buildSessionId, normalizeMachine } from '../session-id.js';
import { isService, isCouncil, loadCustomServices } from '../service-classification.js';
import { toastSuccess, toastError } from '../toast.js';

// Shared state across sessions and services sections
export const activityStates = new Map();

// Merge config-defined custom services into the Services column. Fire-and-forget
// on load; re-render once they arrive so flagged sessions hop to the right group.
loadCustomServices().then((changed) => { if (changed) notifyListeners(); });

let allSessions = [];
// session name → read-only git status ({dirty, ahead, behind, pushed, ...})
// for worktree sessions, populated from /api/worktrees alongside session fetches.
const worktreeGit = new Map();
const listeners = new Set();

// Close-button state: session name → confirm-expiry timer / in-flight kill.
// Lives at module level so it survives the frequent activity re-renders.
const pendingClose = new Map();
const killingSessions = new Set();

// Parents whose nested children are collapsed in the sidebar (issue #448).
// Module-level so collapse state survives the frequent activity re-renders.
// Default is expanded — the whole point is making the relationship visible.
const collapsedParents = new Set();

export function getAllSessions() { return allSessions; }
// Returns an unsubscribe function — load-bearing for consumers with a
// lifecycle shorter than the page (e.g. workspace-window.js opens/closes
// many times), unlike the permanent sidebar-section subscriber that never
// needs to unhook.
export function onSessionsChanged(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
}

// getAllSessions() is only backed by a live fetch once something has called
// initData()+fetchSessions() — normally sessionsSection.mount(), which only
// runs once the sidebar's Sessions accordion is expanded. A consumer that
// needs session data (parent linkage, etc.) without depending on that UI
// state — e.g. the collage's family grouping, #748 — calls this instead.
// Memoized: the first call fetches once and wires the live 'sessions' event
// subscription (via initData); later calls reuse the same promise, so this
// never becomes a second parallel poller.
let sessionsLoadPromise = null;
export function ensureSessionsLoaded() {
    initData();
    if (!sessionsLoadPromise) sessionsLoadPromise = fetchSessions();
    return sessionsLoadPromise;
}

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

// Compact git-status badges for a worktree session. Returns '' for non-worktree
// sessions (those absent from the worktree map). Local git only — no PR state.
function renderGitBadges(name) {
    const git = worktreeGit.get(name);
    if (!git || !git.exists) return '';
    const badges = [];
    if (git.dirty) {
        const n = (git.staged || 0) + (git.unstaged || 0) + (git.untracked || 0);
        badges.push(`<span class="sidebar-git-badge git-dirty" title="${git.staged||0} staged, ${git.unstaged||0} unstaged, ${git.untracked||0} untracked">● dirty${n ? ` ${n}` : ''}</span>`);
    } else {
        badges.push('<span class="sidebar-git-badge git-clean" title="Working tree clean">clean</span>');
    }
    if (!git.upstream) {
        badges.push('<span class="sidebar-git-badge git-unpushed" title="No upstream — branch not pushed">unpushed</span>');
    } else {
        if (git.ahead) badges.push(`<span class="sidebar-git-badge git-ahead" title="${git.ahead} commit(s) ahead of upstream">↑${git.ahead}</span>`);
        if (git.behind) badges.push(`<span class="sidebar-git-badge git-behind" title="${git.behind} commit(s) behind upstream">↓${git.behind}</span>`);
        if (git.pushed && !git.ahead) badges.push('<span class="sidebar-git-badge git-pushed" title="Pushed — upstream up to date">pushed</span>');
    }
    return `<div class="sidebar-session-git">${badges.join('')}</div>`;
}

export function renderCard(s, opts = {}) {
    const name = s.name || '';
    const machine = normalizeMachine(s.machine);
    const id = buildSessionId(name, machine);
    const activity = activityStates.get(name) || s.activity || 'idle';
    const dotClass = activity === 'idle' ? 'dot-idle' : activity === 'processing' ? 'dot-processing' : activity === 'generating' ? 'dot-generating' : 'dot-playing';
    const tags = [];
    if (s.posture) {
        tags.push(`<span class="sidebar-tag">${s.posture}</span>`);
    }
    if (machine) tags.push(`<span class="sidebar-tag">@${machine}</span>`);
    const roles = (s.roles || []).map(r => `<span class="sidebar-tag sidebar-tag-role">${r}</span>`).join('');
    const path = s.path ? s.path.replace(/^\/Users\/[^/]+\//, '~/') : '';
    const tagsHtml = `${tags.join('')}${roles}`;
    // Collapse caret for parents with nested children (issue #448). The count
    // badge keeps the relationship legible even when the children are hidden.
    const childCount = opts.childCount || 0;
    const caret = childCount
        ? `<button class="sidebar-list-item-btn sidebar-children-toggle" data-action="toggle-children" title="${opts.collapsed ? 'Show' : 'Hide'} ${childCount} child session${childCount > 1 ? 's' : ''}">${opts.collapsed ? '▸' : '▾'}</button>`
        : '';
    const childBadge = childCount
        ? `<span class="sidebar-child-count" title="${childCount} child session${childCount > 1 ? 's' : ''}">${childCount}</span>`
        : '';
    return `<div class="sidebar-session-card" data-session="${name}" data-machine="${machine || ''}" data-id="${id}">
        <div class="sidebar-session-row1">
            ${caret}
            <span class="sidebar-activity-dot ${dotClass}" data-session-dot="${name}"></span>
            <span class="sidebar-session-name">${name}</span>
            ${childBadge}
            <button class="sidebar-list-item-btn" data-action="connect" title="Connect">▸</button>
            <button class="sidebar-list-item-btn" data-action="monitor" title="Monitor">👁</button>
            <button class="sidebar-list-item-btn" data-action="review" title="Review diff (approve/deny)">🔍</button>
            <button class="sidebar-list-item-btn" data-action="workspace" title="Open workspace">🛰</button>
            ${opts.closable ? renderCloseButton(name) : ''}
        </div>
        ${path ? `<div class="sidebar-session-row2"><span class="sidebar-session-path">${path}</span></div>` : ''}
        ${tagsHtml ? `<div class="sidebar-session-row3">${tagsHtml}</div>` : ''}
        ${renderGitBadges(name)}
    </div>`;
}

export function updateActivityDot(body, session) {
    const dot = body.querySelector(`[data-session-dot="${CSS.escape(session)}"]`);
    if (!dot) return;
    dot.className = 'sidebar-activity-dot';
    const state = activityStates.get(session) || 'idle';
    dot.classList.add(state === 'idle' ? 'dot-idle' : state === 'processing' ? 'dot-processing' : state === 'generating' ? 'dot-generating' : 'dot-playing');
}

// Group sessions into a parent→children forest (issue #448). A session nests
// under another only when its `parent` (display linkage from the CLI: creator
// recorded at `agentwire new`, else `.agentwire.yml parent:`) is itself present
// in the same list. Sessions whose parent is absent render as top-level roots.
function buildSessionTree(sessions) {
    const byName = new Map(sessions.map(s => [s.name || '', s]));
    const childrenOf = new Map();  // parent name → [child sessions]
    const roots = [];
    for (const s of sessions) {
        const parent = s.parent;
        if (parent && parent !== s.name && byName.has(parent)) {
            if (!childrenOf.has(parent)) childrenOf.set(parent, []);
            childrenOf.get(parent).push(s);
        } else {
            roots.push(s);
        }
    }
    return { childrenOf, roots };
}

// Render one session and its nested children (recursive). `visited` guards
// against pathological parent cycles so a 2-cycle can't loop forever.
function renderSessionNode(s, childrenOf, visited) {
    const name = s.name || '';
    if (visited.has(name)) return '';
    visited.add(name);
    const kids = childrenOf.get(name) || [];
    const collapsed = collapsedParents.has(name);
    const card = renderCard(s, { closable: true, childCount: kids.length, collapsed });
    if (!kids.length) return card;
    const childrenHtml = collapsed ? '' : `<div class="sidebar-session-children">
        <div class="sidebar-session-children-label">Child sessions</div>
        ${kids.map(k => renderSessionNode(k, childrenOf, visited)).join('')}
    </div>`;
    return `<div class="sidebar-session-group">${card}${childrenHtml}</div>`;
}

// Full forest render: roots first, then any session orphaned by a cycle.
function renderSessionForest(sessions) {
    const { childrenOf, roots } = buildSessionTree(sessions);
    const visited = new Set();
    let html = roots.map(s => renderSessionNode(s, childrenOf, visited)).join('');
    for (const s of sessions) {
        if (!visited.has(s.name || '')) html += renderSessionNode(s, childrenOf, visited);
    }
    return html;
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
    if (action === 'review') {
        const { openReviewWindow } = await import('../desktop.js');
        openReviewWindow(session);
        return;
    }
    if (action === 'workspace') {
        const { openSessionWorkspace } = await import('../desktop.js');
        openSessionWorkspace(session, machine);
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
        toastSuccess(`Killed ${session}`);
    } catch (err) {
        toastError(`Failed to kill ${session}: ${err.message}`);
    } finally {
        killingSessions.delete(session);
        notifyListeners();
    }
}

// Data fetching + WebSocket events (registered once by sessionsSection, but
// exported so other consumers of getAllSessions()/activityStates — e.g.
// workspace-window.js — can guarantee the pipeline is live without depending
// on the Sessions sidebar accordion ever being expanded).
let dataInitialized = false;

export function initData() {
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

// Refresh worktree git status (dirty/ahead/behind/pushed) for badge rendering.
// Fire-and-forget — failures just leave the badges absent, never block the list.
async function fetchWorktrees() {
    try {
        const res = await apiFetch('/api/worktrees');
        const data = await res.json();
        worktreeGit.clear();
        for (const e of (data.entries || [])) {
            if (e.session && e.git) worktreeGit.set(e.session, e.git);
        }
        notifyListeners();
    } catch (e) { /* leave badges absent on failure */ }
}

async function fetchSessions() {
    fetchWorktrees();
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
                    toastSuccess(`Created ${sessionName}`);
                    openSessionTerminal(sessionName, 'terminal');
                } else {
                    const err = await res.json().catch(() => ({}));
                    const reason = err.error || `HTTP ${res.status}`;
                    toastError(`Failed to create ${name}: ${reason}`);
                    btn.textContent = err.error || 'Error';
                    setTimeout(() => { btn.disabled = false; btn.textContent = isWorktree ? 'Create worktree' : 'Create'; }, 2000);
                }
            } catch (e) {
                toastError(`Failed to create ${name}: ${e.message}`);
                btn.textContent = 'Error';
                setTimeout(() => { btn.disabled = false; btn.textContent = isWorktree ? 'Create worktree' : 'Create'; }, 2000);
            }
        }
    },

    _render(body) {
        // Council souls/orchestrators live in the Council section, not here.
        const work = allSessions.filter(s => !isService(s.name || '') && !isCouncil(s.name || ''));
        let html = this._renderForm();
        if (!work.length && !this._formType) {
            html += '<div class="sidebar-empty">No sessions</div>';
        } else {
            html += renderSessionForest(work);
        }
        body.innerHTML = html;
        body.onclick = (e) => {
            if (e.target.closest('.sidebar-form')) {
                this._handleFormClick(e, body);
                return;
            }
            const toggle = e.target.closest('[data-action="toggle-children"]');
            if (toggle) {
                const name = toggle.closest('[data-session]')?.dataset.session;
                if (name) {
                    if (collapsedParents.has(name)) collapsedParents.delete(name);
                    else collapsedParents.add(name);
                    this._render(body);
                }
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
