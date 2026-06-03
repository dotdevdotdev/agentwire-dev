/**
 * Command Palette — unified Cmd/Ctrl+K launcher for quick create/open actions.
 *
 * Replaces the standalone quicktask modal. The root view surfaces four actions;
 * each drills into a focused mini-form (or runs directly) without leaving the
 * palette:
 *   - New project   → name + clone URL + git-init → create → spawn session → open
 *   - New session   → pick existing project → spawn session → open
 *   - New worktree  → quicktask flow (project, base, branch, pull-first) → open
 *   - Open session  → pick a running tmux session → attach
 *
 * Keyboard: ↑/↓ navigate, Enter selects, Esc backs out of a drill-in (or closes
 * the palette from the root). Pure frontend reshape — backend endpoints
 * (/api/create, /api/projects/create) are unchanged.
 */

import { normalizeMachine, sameMachine } from './session-id.js';
import { isService } from './sidebar/sessions-section.js';

const PILL_TYPES = ['feat', 'fix', 'chore', 'refactor', 'docs'];
const LS_LAST_PROJECT = 'quicktask:lastProject';
const LS_BASE_PREFIX = 'quicktask:base:';

let paletteEl = null;
let lastFocus = null;
let projectsCache = null;
let sessionsCache = null;
let selectedIndex = 0;
let currentItems = [];           // filtered, runnable items in the active list view
let currentView = 'root';        // 'root' | 'new-project' | 'new-session' | 'worktree' | 'open-session'
let prefillProject = '';

const COMMANDS = [
    { id: 'new-project', icon: '✚', label: 'New project', keywords: 'create new project repo clone git init', run: () => setView('new-project') },
    { id: 'new-session', icon: '▶', label: 'New session', keywords: 'create new session start spawn run project', run: () => setView('new-session') },
    { id: 'worktree', icon: '⎇', label: 'New worktree', keywords: 'worktree branch quicktask task feat fix base', run: () => setView('worktree') },
    { id: 'open-session', icon: '👁', label: 'Open session', keywords: 'open attach connect existing session', run: () => setView('open-session') },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function slugify(text) {
    return String(text)
        .toLowerCase()
        .normalize('NFKD').replace(/[̀-ͯ]/g, '')
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 64);
}

function isSubsequence(needle, haystack) {
    let i = 0;
    for (const ch of haystack) {
        if (ch === needle[i]) i++;
        if (i === needle.length) return true;
    }
    return needle.length === 0;
}

/** Fuzzy/substring match: every whitespace-delimited token must hit the text. */
function matches(query, text) {
    const q = String(query).toLowerCase().trim();
    if (!q) return true;
    const t = String(text).toLowerCase();
    return q.split(/\s+/).every((tok) => t.includes(tok) || isSubsequence(tok, t));
}

async function loadProjects() {
    if (projectsCache) return projectsCache;
    try {
        const res = await fetch('/api/projects');
        const data = await res.json();
        projectsCache = data.projects || [];
    } catch (e) {
        projectsCache = [];
    }
    return projectsCache;
}

async function loadSessions() {
    const out = [];
    try {
        const r = await fetch('/api/sessions/local');
        const d = await r.json();
        out.push(...(d.sessions || []));
    } catch (e) { /* ignore */ }
    try {
        const r = await fetch('/api/sessions/remote');
        const d = await r.json();
        const names = new Set(out.map((s) => s.name));
        for (const s of (d.sessions || [])) {
            if (!names.has(s.name)) out.push(s);
        }
    } catch (e) { /* ignore */ }
    sessionsCache = out.filter((s) => !isService(s.name || ''));
    return sessionsCache;
}

async function openTerminal(name, mode, machine) {
    const { openSessionTerminal } = await import('./desktop.js');
    openSessionTerminal(name, mode, machine);
}

/** Resume the project's session if one already exists, otherwise create it; then open + focus. */
async function spawnAndOpen({ name, path, machine }) {
    if (!name) throw new Error('Missing session name');
    machine = normalizeMachine(machine);

    // Fast path: resume an existing session of the same name.
    try {
        const url = machine
            ? `/api/sessions/remote?machine=${encodeURIComponent(machine)}`
            : '/api/sessions/local';
        const r = await fetch(url);
        const d = await r.json().catch(() => ({}));
        const sessions = d.sessions || (d.machines || []).flatMap((m) => m.sessions || []);
        if (sessions.some((s) => s.name === name && sameMachine(s.machine, machine))) {
            closeCommandPalette();
            await openTerminal(name, 'terminal', machine);
            return;
        }
    } catch (e) { /* fall through and create */ }

    const res = await fetch('/api/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, path, machine }),
    });
    const data = await res.json().catch(() => ({}));
    const err = data.error || '';
    if (!res.ok || (err && !/already exists/i.test(err))) {
        throw new Error(err || `Create failed (HTTP ${res.status})`);
    }
    closeCommandPalette();
    await openTerminal(data.session || data.name || name, 'terminal', machine);
}

// ---------------------------------------------------------------------------
// Body message helpers (error / progress) — reuse quicktask modal styling
// ---------------------------------------------------------------------------

function showError(text) {
    if (!paletteEl) return;
    const el = paletteEl.querySelector('[data-error]');
    if (el) { el.textContent = text; el.hidden = false; }
    const prog = paletteEl.querySelector('[data-progress]');
    if (prog) prog.hidden = true;
    const form = paletteEl.querySelector('.quicktask-form');
    if (form) form.hidden = false;
}

function showProgress(label) {
    if (!paletteEl) return;
    const el = paletteEl.querySelector('[data-progress]');
    if (el) {
        el.innerHTML = `
            <div class="quicktask-spinner" aria-hidden="true"></div>
            <div class="quicktask-progress-label">${escapeHtml(label)}</div>`;
        el.hidden = false;
    }
    const errEl = paletteEl.querySelector('[data-error]');
    if (errEl) errEl.hidden = true;
    const form = paletteEl.querySelector('.quicktask-form');
    if (form) form.hidden = true;
}

// ---------------------------------------------------------------------------
// Form views
// ---------------------------------------------------------------------------

function projectOptionsHtml() {
    return (projectsCache || [])
        .map((p) => `<option value="${escapeHtml(p.name)}">`)
        .join('');
}

function findProject(name) {
    return (projectsCache || []).find((p) => p.name === name) || null;
}

function newProjectFormHtml() {
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="new-project">
            <label class="quicktask-field">
                <span class="quicktask-label">Project name</span>
                <input type="text" name="name" placeholder="my-project" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" autocomplete="off" required />
            </label>
            <label class="quicktask-field">
                <span class="quicktask-label">Clone URL <em>(optional)</em></span>
                <input type="text" name="clone_url" placeholder="git@github.com:owner/repo.git" autocomplete="off" />
            </label>
            <label class="quicktask-checkbox">
                <input type="checkbox" name="git_init" checked />
                <span>Initialize empty git repository (skipped when cloning)</span>
            </label>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Create + Open</button>
            </div>
        </form>`;
}

function bindNewProjectForm(form) {
    const nameInput = form.querySelector('input[name="name"]');
    const urlInput = form.querySelector('input[name="clone_url"]');
    let userEditedName = false;
    nameInput?.addEventListener('input', () => { userEditedName = true; });
    urlInput?.addEventListener('input', () => {
        if (userEditedName) return;
        const m = String(urlInput.value).trim().match(/\/([^/]+?)(?:\.git)?\/?$/);
        if (m) nameInput.value = m[1];
    });
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = nameInput.value.trim();
        const cloneUrl = urlInput.value.trim();
        const gitInit = form.querySelector('input[name="git_init"]').checked;
        if (!name) { showError('Project name is required.'); return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
            showError("Invalid name (allowed: letters, digits, '.', '_', '-').");
            return;
        }
        showProgress(cloneUrl ? `Cloning ${cloneUrl}…` : `Creating ${name}…`);
        try {
            const res = await fetch('/api/projects/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, clone_url: cloneUrl || undefined, git_init: gitInit }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) {
                showError(data.error || `Create failed (HTTP ${res.status})`);
                return;
            }
            projectsCache = null;  // invalidate so the new project shows next time
            showProgress(`Starting session for ${data.name}…`);
            await spawnAndOpen({ name: data.name, path: data.path, machine: data.machine });
        } catch (err) {
            showError(err?.message || 'Network error');
        }
    });
}

function newSessionFormHtml() {
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="new-session">
            <label class="quicktask-field">
                <span class="quicktask-label">Project</span>
                <input type="text" name="project" list="cmdkProjects" value="${escapeHtml(prefillProject)}" autocomplete="off" required />
                <datalist id="cmdkProjects">${projectOptionsHtml()}</datalist>
            </label>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Start + Open</button>
            </div>
        </form>`;
}

function bindNewSessionForm(form) {
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = form.querySelector('input[name="project"]').value.trim();
        if (!name) { showError('Pick a project.'); return; }
        const proj = findProject(name);
        showProgress(`Starting session for ${name}…`);
        try {
            await spawnAndOpen({ name, path: proj?.path, machine: proj?.machine });
        } catch (err) {
            showError(err?.message || 'Network error');
        }
    });
}

function worktreeFormHtml() {
    const lastProject = prefillProject || localStorage.getItem(LS_LAST_PROJECT) || '';
    const baseFor = (proj) => localStorage.getItem(LS_BASE_PREFIX + proj) || 'main';
    const pillsHtml = PILL_TYPES.map((t) => `<button type="button" class="quicktask-pill" data-prefix="${t}">${t}</button>`).join('');
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="worktree">
            <label class="quicktask-field">
                <span class="quicktask-label">Project</span>
                <input type="text" name="project" list="cmdkProjects" value="${escapeHtml(lastProject)}" autocomplete="off" required />
                <datalist id="cmdkProjects">${projectOptionsHtml()}</datalist>
            </label>
            <label class="quicktask-field">
                <span class="quicktask-label">Base branch</span>
                <input type="text" name="base" value="${escapeHtml(baseFor(lastProject))}" autocomplete="off" required />
            </label>
            <label class="quicktask-field">
                <span class="quicktask-label">Task title <em>(optional)</em></span>
                <input type="text" name="title" placeholder="Voice fix bug" autocomplete="off" />
            </label>
            <div class="quicktask-field">
                <span class="quicktask-label">New branch</span>
                <div class="quicktask-pills">${pillsHtml}</div>
                <input type="text" name="branch" placeholder="feat/voice-fix-bug" autocomplete="off" required />
            </div>
            <label class="quicktask-checkbox">
                <input type="checkbox" name="pull_first" checked />
                <span>Pull base from origin first</span>
            </label>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Create + Open</button>
            </div>
        </form>`;
}

function bindWorktreeForm(form) {
    const titleInput = form.querySelector('input[name="title"]');
    const branchInput = form.querySelector('input[name="branch"]');
    const projectInput = form.querySelector('input[name="project"]');
    const baseInput = form.querySelector('input[name="base"]');

    let userEditedBranch = false;
    branchInput.addEventListener('input', () => { userEditedBranch = true; });

    titleInput?.addEventListener('input', () => {
        if (userEditedBranch) return;
        const slug = slugify(titleInput.value);
        const prefixMatch = branchInput.value.match(/^([a-z]+)\//);
        branchInput.value = prefixMatch ? `${prefixMatch[1]}/${slug}` : slug;
    });

    form.querySelectorAll('.quicktask-pill').forEach((btn) => {
        btn.addEventListener('click', () => {
            const stripped = branchInput.value.replace(/^[a-z]+\//, '');
            branchInput.value = `${btn.dataset.prefix}/${stripped}`;
            branchInput.focus();
        });
    });

    projectInput?.addEventListener('change', () => {
        const proj = projectInput.value.trim();
        const stored = proj && localStorage.getItem(LS_BASE_PREFIX + proj);
        if (stored) baseInput.value = stored;
    });

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const project = projectInput.value.trim();
        const base = baseInput.value.trim() || 'main';
        const branch = branchInput.value.trim();
        const pullFirst = form.querySelector('input[name="pull_first"]').checked;
        if (!project || !branch) { showError('Project and new branch are required.'); return; }

        localStorage.setItem(LS_LAST_PROJECT, project);
        localStorage.setItem(LS_BASE_PREFIX + project, base);
        const proj = findProject(project);
        showProgress(pullFirst ? `Pulling ${base} and starting ${branch}…` : `Starting ${branch}…`);
        try {
            const res = await fetch('/api/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: project,
                    machine: proj?.machine,
                    worktree: true,
                    branch,
                    base,
                    pull_first: pullFirst,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.error) {
                showError(data.error || `Create failed (HTTP ${res.status})`);
                return;
            }
            const sessionName = data.session || data.name || `${project}/${branch}`;
            closeCommandPalette();
            await openTerminal(sessionName, 'terminal', proj?.machine);
        } catch (err) {
            showError(err?.message || 'Network error');
        }
    });
}

// ---------------------------------------------------------------------------
// List views (root + open-session)
// ---------------------------------------------------------------------------

function rootItems(query) {
    return COMMANDS
        .filter((c) => matches(query, `${c.label} ${c.keywords}`))
        .map((c) => ({ icon: c.icon, label: c.label, run: c.run }));
}

function openSessionItems(query) {
    return (sessionsCache || [])
        .filter((s) => matches(query, s.name))
        .map((s) => ({
            icon: '👁',
            label: s.name,
            sublabel: s.machine ? `@${s.machine}` : '',
            run: async () => {
                closeCommandPalette();
                await openTerminal(s.name, 'terminal', normalizeMachine(s.machine));
            },
        }));
}

function renderListView(items) {
    currentItems = items;
    if (selectedIndex >= items.length) selectedIndex = Math.max(0, items.length - 1);
    const body = paletteEl.querySelector('.cmdk-body');
    if (!items.length) {
        body.innerHTML = '<div class="cmdk-empty">No matches</div>';
        return;
    }
    body.innerHTML = items.map((it, i) => `
        <div class="cmdk-item${i === selectedIndex ? ' cmdk-item-selected' : ''}" data-index="${i}">
            <span class="cmdk-item-icon">${it.icon || ''}</span>
            <span class="cmdk-item-label">${escapeHtml(it.label)}</span>
            ${it.sublabel ? `<span class="cmdk-item-sub">${escapeHtml(it.sublabel)}</span>` : ''}
        </div>`).join('');
}

function updateSelection() {
    paletteEl.querySelectorAll('.cmdk-item').forEach((el, i) => {
        el.classList.toggle('cmdk-item-selected', i === selectedIndex);
    });
    const sel = paletteEl.querySelector('.cmdk-item-selected');
    sel?.scrollIntoView({ block: 'nearest' });
}

const LIST_VIEWS = { root: rootItems, 'open-session': openSessionItems };

function isListView() {
    return Object.prototype.hasOwnProperty.call(LIST_VIEWS, currentView);
}

// ---------------------------------------------------------------------------
// View orchestration
// ---------------------------------------------------------------------------

function renderView() {
    const search = paletteEl.querySelector('.cmdk-search');
    const input = paletteEl.querySelector('.cmdk-input');
    const footer = paletteEl.querySelector('.cmdk-footer');
    const body = paletteEl.querySelector('.cmdk-body');

    if (isListView()) {
        search.hidden = false;
        input.placeholder = currentView === 'open-session' ? 'Search sessions…' : 'Type a command or search…';
        footer.textContent = '↑↓ navigate · ↵ select · esc ' + (currentView === 'root' ? 'close' : 'back');
        renderListView(LIST_VIEWS[currentView](input.value));
        return;
    }

    // Form views: hide the filter input, render the mini-form.
    search.hidden = true;
    currentItems = [];
    footer.textContent = '↵ submit · esc back';
    if (currentView === 'new-project') body.innerHTML = newProjectFormHtml();
    else if (currentView === 'new-session') body.innerHTML = newSessionFormHtml();
    else if (currentView === 'worktree') body.innerHTML = worktreeFormHtml();
    const form = body.querySelector('.quicktask-form');
    if (currentView === 'new-project') bindNewProjectForm(form);
    else if (currentView === 'new-session') bindNewSessionForm(form);
    else if (currentView === 'worktree') bindWorktreeForm(form);
}

function focusActiveInput() {
    if (isListView()) {
        paletteEl.querySelector('.cmdk-input')?.focus();
    } else {
        paletteEl.querySelector('.quicktask-form input')?.focus();
    }
}

async function setView(view) {
    currentView = view;
    selectedIndex = 0;
    const input = paletteEl.querySelector('.cmdk-input');
    input.value = '';
    if (view === 'open-session') await loadSessions();
    if (view === 'new-session' || view === 'worktree') await loadProjects();
    renderView();
    focusActiveInput();
}

function goBack() {
    if (currentView === 'root') {
        closeCommandPalette();
        return;
    }
    prefillProject = '';
    setView('root');
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function attachListeners() {
    const input = paletteEl.querySelector('.cmdk-input');
    const body = paletteEl.querySelector('.cmdk-body');

    input.addEventListener('input', () => {
        selectedIndex = 0;
        if (isListView()) renderListView(LIST_VIEWS[currentView](input.value));
    });

    paletteEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            goBack();
            return;
        }
        if (!isListView()) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (currentItems.length) { selectedIndex = (selectedIndex + 1) % currentItems.length; updateSelection(); }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (currentItems.length) { selectedIndex = (selectedIndex - 1 + currentItems.length) % currentItems.length; updateSelection(); }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            currentItems[selectedIndex]?.run();
        }
    });

    body.addEventListener('mousemove', (e) => {
        const item = e.target.closest('.cmdk-item');
        if (!item) return;
        const idx = Number(item.dataset.index);
        if (idx !== selectedIndex) { selectedIndex = idx; updateSelection(); }
    });

    body.addEventListener('click', (e) => {
        const item = e.target.closest('.cmdk-item');
        if (item) { currentItems[Number(item.dataset.index)]?.run(); return; }
        const action = e.target.closest('[data-action]')?.dataset.action;
        if (action === 'back') goBack();
    });

    paletteEl.addEventListener('click', (e) => {
        if (e.target === paletteEl) closeCommandPalette();
    });
}

function shellHtml() {
    return `<div class="modal-overlay cmdk-overlay" id="commandPaletteOverlay">
        <div class="modal command-palette" role="dialog" aria-label="Command palette">
            <div class="cmdk-search">
                <span class="cmdk-search-icon" aria-hidden="true">⌕</span>
                <input class="cmdk-input" type="text" autocomplete="off" spellcheck="false" placeholder="Type a command or search…" aria-label="Command palette search" />
            </div>
            <div class="cmdk-body"></div>
            <div class="cmdk-footer"></div>
        </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openCommandPalette({ view = 'root', project = '' } = {}) {
    if (paletteEl) return;
    lastFocus = document.activeElement;
    selectedIndex = 0;
    prefillProject = project;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = shellHtml();
    paletteEl = wrapper.firstElementChild;
    document.body.appendChild(paletteEl);
    attachListeners();
    await setView(view);
}

export function closeCommandPalette() {
    if (!paletteEl) return;
    paletteEl.remove();
    paletteEl = null;
    currentView = 'root';
    currentItems = [];
    selectedIndex = 0;
    prefillProject = '';
    if (lastFocus && typeof lastFocus.focus === 'function') {
        try { lastFocus.focus(); } catch (e) { /* ignore */ }
    }
    lastFocus = null;
}

export function isCommandPaletteOpen() {
    return paletteEl !== null;
}
