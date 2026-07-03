/**
 * Command Palette — unified Cmd/Ctrl+K launcher for quick create/open actions.
 *
 * Cmd/Ctrl+K opens the root list with "Ask council" selected by default (Esc
 * closes). Root view actions:
 *   - Ask council   → open the council board to seat/ask a question
 *   - New idea      → idea (typed or dictated) + derived name → create project
 *                     → spawn session → idea delivered as the agent's first
 *                     message → open (you watch it land live)
 *   - New session   → pick existing project → spawn session → open
 *   - New worktree  → quicktask flow (project, base, branch, pull-first) → open
 *   - Open session  → pick a running tmux session → attach
 *
 * User-defined items (config.yaml palette.items, #676) render alongside the
 * built-ins: no fields → run immediately (toast reports the outcome); with
 * fields → a mini-form, then POST /api/palette/run.
 *
 * Keyboard: ↑/↓ navigate, Enter selects, Esc backs out of a drill-in (or closes
 * the palette from the root).
 */

import { apiFetch } from './api.js';
import { normalizeMachine, sameMachine } from './session-id.js';
import { isService, isCouncil } from './service-classification.js';
import * as browserStt from './voice/browser-stt.js';

const PILL_TYPES = ['feat', 'fix', 'chore', 'refactor', 'docs'];
const LS_LAST_PROJECT = 'quicktask:lastProject';
const LS_BASE_PREFIX = 'quicktask:base:';

let paletteEl = null;
let lastFocus = null;
let projectsCache = null;
let sessionsCache = null;
let customCommandsCache = null;  // user-defined items from ~/.agentwire/config.yaml (#676)
let selectedIndex = 0;
let currentItems = [];           // filtered, runnable items in the active list view
let currentView = 'root';        // 'root' | 'new-idea' | 'new-session' | 'worktree' | 'open-session' | 'custom-item'
let prefillProject = '';
let currentCustomItem = null;    // the custom item whose field form is open

const COMMANDS = [
    { id: 'ask-council', icon: '🏛', label: 'Ask council', keywords: 'council ask question deliberate lenses brainstorm advice decide soul', run: () => setView('ask-council') },
    { id: 'new-idea', icon: '💡', label: 'New idea', keywords: 'idea create new project repo clone git init build start', run: () => setView('new-idea') },
    { id: 'new-session', icon: '▶', label: 'New session', keywords: 'create new session start spawn run project', run: () => setView('new-session') },
    { id: 'worktree', icon: '⎇', label: 'New worktree', keywords: 'worktree branch quicktask task feat fix base', run: () => setView('worktree') },
    { id: 'open-session', icon: '👁', label: 'Open session', keywords: 'open attach connect existing session', run: () => setView('open-session') },
    { id: 'collage', icon: '▦', label: 'Window collage', keywords: 'collage cascade grid windows overview show all tile mission', run: async () => {
        closeCommandPalette();
        const { collage } = await import('./collage.js');
        collage.enter();
    } },
    { id: 'help', icon: '⌨', label: 'Keyboard shortcuts & help', keywords: 'help shortcuts keys keyboard cheat sheet f1 bindings features guide', run: async () => {
        closeCommandPalette();
        const { openHelp } = await import('./help-modal.js');
        openHelp();
    } },
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

// Filler words stripped when deriving a project name from an idea.
const NAME_STOPWORDS = new Set([
    'a', 'an', 'the', 'i', 'we', 'me', 'my', 'us', 'it', 'is', 'in', 'on', 'of',
    'to', 'for', 'and', 'or', 'that', 'this', 'with', 'some', 'about', 'like',
    'just', 'really', 'can', 'could', 'should', 'would', 'please', 'want',
    'wanna', 'need', 'lets', 'let', 'make', 'build', 'create', 'new', 'app',
    'project', 'tool', 'little', 'simple', 'basic',
]);

/** Derive an editable kebab-case project name from a free-text idea. */
function deriveProjectName(idea) {
    const words = String(idea).toLowerCase()
        .replace(/[^a-z0-9\s-]/g, ' ')
        .split(/\s+/).filter(Boolean);
    const kept = words.filter((w) => !NAME_STOPWORDS.has(w)).slice(0, 4);
    return slugify((kept.length ? kept : words.slice(0, 3)).join('-')).slice(0, 40).replace(/-+$/, '');
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
        const res = await apiFetch('/api/projects');
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
        const r = await apiFetch('/api/sessions/local');
        const d = await r.json();
        out.push(...(d.sessions || []));
    } catch (e) { /* ignore */ }
    try {
        const r = await apiFetch('/api/sessions/remote');
        const d = await r.json();
        const names = new Set(out.map((s) => s.name));
        for (const s of (d.sessions || [])) {
            if (!names.has(s.name)) out.push(s);
        }
    } catch (e) { /* ignore */ }
    sessionsCache = out.filter((s) => !isService(s.name || '') && !isCouncil(s.name || ''));
    return sessionsCache;
}

/** User-defined palette items (config.yaml palette.items). Best-effort:
 *  a portal without the endpoint or an empty config just yields []. */
async function loadCustomCommands() {
    if (customCommandsCache) return customCommandsCache;
    try {
        const res = await apiFetch('/api/palette');
        const data = await res.json();
        customCommandsCache = res.ok ? (data.items || []) : [];
    } catch (e) {
        customCommandsCache = [];
    }
    return customCommandsCache;
}

/** Execute a custom item server-side and report the outcome as a toast. */
async function runCustomItem(item, fields) {
    const { toastSuccess, toastError } = await import('./toast.js');
    try {
        const res = await apiFetch('/api/palette/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: item.id, fields }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.success === false) {
            throw new Error(data.error || (data.output || '').trim() || `Run failed (HTTP ${res.status})`);
        }
        const out = (data.output || '').trim();
        toastSuccess(`${item.label} ✓${out ? `\n${out.slice(0, 400)}` : ''}`);
        return true;
    } catch (err) {
        toastError(`${item.label}: ${err?.message || 'failed'}`);
        return false;
    }
}

async function openTerminal(name, mode, machine) {
    const { openSessionTerminal } = await import('./desktop.js');
    openSessionTerminal(name, mode, machine);
}

/** Resume the project's session if one already exists, otherwise create it; then open + focus.
 * `firstMessage` is delivered to the agent in the background once it boots
 * (fresh sessions only — a resumed session ignores it). */
async function spawnAndOpen({ name, path, machine, firstMessage, roles, posture, harness }) {
    if (!name) throw new Error('Missing session name');
    machine = normalizeMachine(machine);

    // Fast path: resume an existing session of the same name.
    try {
        const url = machine
            ? `/api/sessions/remote?machine=${encodeURIComponent(machine)}`
            : '/api/sessions/local';
        const r = await apiFetch(url);
        const d = await r.json().catch(() => ({}));
        const sessions = d.sessions || (d.machines || []).flatMap((m) => m.sessions || []);
        if (sessions.some((s) => s.name === name && sameMachine(s.machine, machine))) {
            closeCommandPalette();
            await openTerminal(name, 'terminal', machine);
            return;
        }
    } catch (e) { /* fall through and create */ }

    const res = await apiFetch('/api/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name, path, machine,
            first_message: firstMessage || undefined,
            roles: (roles && roles.length) ? roles : undefined,
            posture: posture || undefined,
            harness: harness || undefined,
        }),
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

function newIdeaFormHtml() {
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="new-idea">
            <label class="quicktask-field">
                <span class="quicktask-label">Idea</span>
                <div class="cmdk-idea-wrap">
                    <textarea name="idea" class="cmdk-idea-input" rows="3"
                        placeholder="What do you want to build?"
                        autocomplete="off" spellcheck="false"></textarea>
                    <button type="button" class="cmdk-idea-mic" data-action="mic"
                        title="Dictate your idea" aria-label="Dictate your idea" hidden>🎤</button>
                </div>
            </label>
            <label class="quicktask-field">
                <span class="quicktask-label">Project name <em>(derived — edit if you like)</em></span>
                <input type="text" name="name" placeholder="my-project" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" autocomplete="off" required />
            </label>
            <label class="quicktask-field">
                <span class="quicktask-label">Clone URL <em>(optional — start from an existing repo)</em></span>
                <input type="text" name="clone_url" placeholder="git@github.com:owner/repo.git" autocomplete="off" />
            </label>
            <label class="quicktask-checkbox">
                <input type="checkbox" name="git_init" checked />
                <span>Initialize empty git repository (skipped when cloning)</span>
            </label>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Create &amp; run</button>
            </div>
        </form>`;
}

function bindNewIdeaForm(form) {
    const ideaInput = form.querySelector('textarea[name="idea"]');
    const nameInput = form.querySelector('input[name="name"]');
    const urlInput = form.querySelector('input[name="clone_url"]');
    const micBtn = form.querySelector('.cmdk-idea-mic');
    let userEditedName = false;

    nameInput?.addEventListener('input', () => { userEditedName = true; });

    // Idea drives the project name until the user takes over.
    ideaInput?.addEventListener('input', () => {
        if (userEditedName) return;
        nameInput.value = deriveProjectName(ideaInput.value);
    });

    // Enter submits (one breath, one keystroke); Shift+Enter for a newline.
    ideaInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            form.requestSubmit();
        }
    });

    // Clone URL fills the name from the repo (same takeover rule).
    urlInput?.addEventListener('input', () => {
        if (userEditedName) return;
        const m = String(urlInput.value).trim().match(/\/([^/]+?)(?:\.git)?\/?$/);
        if (m) nameInput.value = m[1];
    });

    // Dictation — browser STT (Chrome). Click to start, click again to stop.
    if (micBtn && browserStt.isSupported()) {
        micBtn.hidden = false;
        let recording = false;
        const baseText = { value: '' };
        micBtn.addEventListener('click', () => {
            if (recording) { browserStt.stop(); return; }
            baseText.value = ideaInput.value ? ideaInput.value.trimEnd() + ' ' : '';
            const started = browserStt.start({
                onInterim: (text) => {
                    ideaInput.value = baseText.value + text;
                },
                onFinal: (text) => {
                    recording = false;
                    micBtn.classList.remove('cmdk-idea-mic-recording');
                    ideaInput.value = (baseText.value + text).trim();
                    ideaInput.dispatchEvent(new Event('input'));
                    ideaInput.focus();
                },
                onError: () => {
                    recording = false;
                    micBtn.classList.remove('cmdk-idea-mic-recording');
                },
            });
            if (started) {
                recording = true;
                micBtn.classList.add('cmdk-idea-mic-recording');
            }
        });
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const idea = ideaInput.value.trim();
        const name = nameInput.value.trim();
        const cloneUrl = urlInput.value.trim();
        const gitInit = form.querySelector('input[name="git_init"]').checked;
        if (!name) { showError(idea ? 'Could not derive a name — type one.' : 'Type an idea (or a project name).'); return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
            showError("Invalid name (allowed: letters, digits, '.', '_', '-').");
            return;
        }
        showProgress(cloneUrl ? `Cloning ${cloneUrl}…` : `Creating ${name}…`);
        try {
            const res = await apiFetch('/api/projects/create', {
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
            showProgress(idea ? 'Starting the agent with your idea…' : `Starting session for ${data.name}…`);
            await spawnAndOpen({
                name: data.name,
                path: data.path,
                machine: data.machine,
                firstMessage: idea || undefined,
            });
        } catch (err) {
            showError(err?.message || 'Network error');
        }
    });
}

// Posture options for the advanced fold (the resolver hands us the default).
const POSTURE_OPTIONS = ['bypass', 'prompted', 'restricted', 'readonly'];

function newSessionFormHtml() {
    // One-click default: just a project. Roles (resolved chips) and the
    // posture/harness fold are populated/wired in bindNewSessionForm — the
    // form reads /api/session/defaults, never hardcoding the resolver.
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="new-session">
            <label class="quicktask-field">
                <span class="quicktask-label">Project</span>
                <input type="text" name="project" list="cmdkProjects" value="${escapeHtml(prefillProject)}" autocomplete="off" required />
                <datalist id="cmdkProjects">${projectOptionsHtml()}</datalist>
            </label>
            <div class="quicktask-field">
                <span class="quicktask-label">Roles <em>(intrinsic etiquette — add your own, remove any)</em></span>
                <div class="quicktask-pills" data-roles-chips></div>
                <input type="text" data-role-add placeholder="add a role + Enter" autocomplete="off" />
            </div>
            <details class="cmdk-advanced">
                <summary>Advanced</summary>
                <label class="quicktask-field">
                    <span class="quicktask-label">Posture</span>
                    <select name="posture" data-posture></select>
                </label>
                <label class="quicktask-field">
                    <span class="quicktask-label">Harness</span>
                    <input type="text" name="harness" data-harness placeholder="claude" autocomplete="off" />
                </label>
                <div class="cmdk-resolved" data-resolved></div>
            </details>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Start + Open</button>
            </div>
        </form>`;
}

function bindNewSessionForm(form) {
    const chipsEl = form.querySelector('[data-roles-chips]');
    const addInput = form.querySelector('[data-role-add]');
    const postureSel = form.querySelector('[data-posture]');
    const harnessInput = form.querySelector('[data-harness]');
    const resolvedEl = form.querySelector('[data-resolved]');

    // Roles split into intrinsic (from the resolver, shown but the user can
    // still drop them) and user-added. We submit the full remaining set.
    let roles = [];

    function renderChips() {
        chipsEl.innerHTML = roles.map((r, i) =>
            `<button type="button" class="quicktask-pill cmdk-chip" data-i="${i}">${escapeHtml(r)} ✕</button>`
        ).join('');
        chipsEl.querySelectorAll('.cmdk-chip').forEach((btn) => {
            btn.addEventListener('click', () => {
                roles.splice(Number(btn.dataset.i), 1);
                renderChips();
            });
        });
    }

    async function refreshDefaults() {
        const posture = postureSel.value || '';
        const harness = (harnessInput.value || '').trim();
        const qs = new URLSearchParams({ kind: 'orchestrator' });
        if (posture) qs.set('posture', posture);
        if (harness) qs.set('harness', harness);
        try {
            const res = await apiFetch(`/api/session/defaults?${qs}`);
            const d = await res.json().catch(() => ({}));
            if (!res.ok || d.error) return;
            // Seed posture options once from the resolver.
            if (!postureSel.options.length) {
                const opts = (d.postures || POSTURE_OPTIONS);
                postureSel.innerHTML = opts.map((p) =>
                    `<option value="${p}"${p === d.posture ? ' selected' : ''}>${p}</option>`).join('');
            }
            if (!harnessInput.value) harnessInput.placeholder = d.harness || 'claude';
            // Seed intrinsic role chips on first load only (don't stomp edits).
            if (roles.length === 0) { roles = [...(d.roles || [])]; renderChips(); }
            resolvedEl.textContent = `→ ${d.session_type}`;
        } catch (e) { /* leave defaults as-is */ }
    }

    addInput?.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const v = addInput.value.trim();
        if (v && !roles.includes(v)) { roles.push(v); renderChips(); }
        addInput.value = '';
    });
    postureSel?.addEventListener('change', refreshDefaults);
    harnessInput?.addEventListener('change', refreshDefaults);

    refreshDefaults();

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = form.querySelector('input[name="project"]').value.trim();
        if (!name) { showError('Pick a project.'); return; }
        const proj = findProject(name);
        showProgress(`Starting session for ${name}…`);
        try {
            await spawnAndOpen({
                name, path: proj?.path, machine: proj?.machine,
                roles, posture: postureSel.value || undefined,
                harness: (harnessInput.value || '').trim() || undefined,
            });
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
            const res = await apiFetch('/api/create', {
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
// Ask council — question-first front door (seat-if-needed → ask → watch board)
// ---------------------------------------------------------------------------

function askCouncilFormHtml() {
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="ask-council">
            <label class="quicktask-field">
                <span class="quicktask-label">Ask the council</span>
                <textarea name="prompt" class="cmdk-idea-input" rows="4"
                    placeholder="A question or decision to put to the lenses…"
                    autocomplete="off" spellcheck="false"></textarea>
            </label>
            <p class="cmdk-council-note">One model, six lenses — structured self-critique, not a panel of experts. Seats a council if none is live (~6 sessions).</p>
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Convene &amp; ask</button>
            </div>
        </form>`;
}

function bindAskCouncilForm(form) {
    const input = form.querySelector('textarea[name="prompt"]');
    // Enter submits; Shift+Enter for a newline. Escape must bubble to the
    // palette's overlay handler (goBack), so don't swallow it.
    input?.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') return;
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
        e.stopPropagation();  // keep other keystrokes off global shortcuts
    });
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const prompt = input.value.trim();
        if (!prompt) { input.focus(); return; }
        await submitAskCouncil(prompt);
    });
}

/** Resolve a sitting (seat one if none live), fan the prompt out, open the board.
 *  Fire-and-forget: we don't wait for the takes — the board fills them live. */
async function submitAskCouncil(prompt) {
    showProgress('Convening the council…');
    try {
        let sitting = null;
        const sres = await apiFetch('/api/council/sittings');
        const live = (sres.ok ? (await sres.json()).sittings : []) || [];
        if (live.length === 1) {
            sitting = live[0];
        } else if (live.length === 0) {
            showProgress('Seating the council…');
            const st = await apiFetch('/api/council/start', { method: 'POST' });
            const sd = await st.json().catch(() => ({}));
            if (!st.ok) { showError(sd.error || 'Could not seat the council.'); return; }
            sitting = sd.council || null;
        } else {
            // Several live — let the user pick which chamber in the board.
            closeCommandPalette();
            const { openCouncilWindow } = await import('./desktop.js');
            openCouncilWindow(null);
            return;
        }
        showProgress('Asking the council…');
        const body = { prompt };
        if (sitting) body.sitting = sitting;
        const ares = await apiFetch('/api/council/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const adata = await ares.json().catch(() => ({}));
        if (!ares.ok) { showError(adata.error || 'Could not ask the council.'); return; }
        closeCommandPalette();
        const { openCouncilWindow } = await import('./desktop.js');
        openCouncilWindow(sitting || adata.council || null);
    } catch (err) {
        showError(err?.message || 'Network error');
    }
}

// ---------------------------------------------------------------------------
// List views (root + open-session)
// ---------------------------------------------------------------------------

function rootItems(query) {
    const builtins = COMMANDS
        .filter((c) => matches(query, `${c.label} ${c.keywords}`))
        .map((c) => ({ icon: c.icon, label: c.label, run: c.run }));
    const customs = (customCommandsCache || [])
        .filter((c) => matches(query, `${c.label} ${c.keywords || ''} ${c.id}`))
        .map((c) => ({
            icon: c.icon || '⚡',
            label: c.label,
            run: async () => {
                if ((c.fields || []).length) {
                    currentCustomItem = c;
                    setView('custom-item');
                } else {
                    closeCommandPalette();
                    runCustomItem(c, {});
                }
            },
        }));
    return [...builtins, ...customs];
}

function customItemFormHtml(item) {
    const fieldsHtml = (item.fields || []).map((f) => `
        <label class="quicktask-field">
            <span class="quicktask-label">${escapeHtml(f.label || f.name)}</span>
            <input type="text" name="${escapeHtml(f.name)}" value="${escapeHtml(f.default || '')}" autocomplete="off" />
        </label>`).join('');
    return `
        <div class="quicktask-error" data-error hidden></div>
        <div class="quicktask-progress" data-progress hidden></div>
        <form class="quicktask-form" data-form="custom-item">
            ${fieldsHtml}
            <div class="quicktask-footer">
                <button type="button" class="quicktask-btn-cancel" data-action="back">Back</button>
                <button type="submit" class="quicktask-btn-submit">Run</button>
            </div>
        </form>`;
}

function bindCustomItemForm(form) {
    const item = currentCustomItem;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fields = {};
        for (const f of item.fields || []) {
            fields[f.name] = form.querySelector(`input[name="${CSS.escape(f.name)}"]`)?.value.trim() || '';
        }
        const missing = (item.fields || []).filter((f) => !fields[f.name]);
        if (missing.length) {
            showError(`Missing: ${missing.map((f) => f.label || f.name).join(', ')}`);
            return;
        }
        showProgress(`Running ${item.label}…`);
        const ok = await runCustomItem(item, fields);
        if (ok) closeCommandPalette();
        else if (paletteEl) showError('Failed — see toast for details.');
    });
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
    if (currentView === 'new-idea') {
        footer.textContent = '↵ create & run · ⇧↵ newline · esc back';
        body.innerHTML = newIdeaFormHtml();
    } else if (currentView === 'ask-council') {
        footer.textContent = '↵ convene & ask · ⇧↵ newline · esc back';
        body.innerHTML = askCouncilFormHtml();
    } else if (currentView === 'new-session') body.innerHTML = newSessionFormHtml();
    else if (currentView === 'worktree') body.innerHTML = worktreeFormHtml();
    else if (currentView === 'custom-item') {
        footer.textContent = '↵ run · esc back';
        body.innerHTML = customItemFormHtml(currentCustomItem);
    }
    const form = body.querySelector('.quicktask-form');
    if (currentView === 'new-idea') bindNewIdeaForm(form);
    else if (currentView === 'ask-council') bindAskCouncilForm(form);
    else if (currentView === 'new-session') bindNewSessionForm(form);
    else if (currentView === 'worktree') bindWorktreeForm(form);
    else if (currentView === 'custom-item') bindCustomItemForm(form);
}

function focusActiveInput() {
    if (isListView()) {
        paletteEl.querySelector('.cmdk-input')?.focus();
    } else {
        paletteEl.querySelector('.quicktask-form textarea, .quicktask-form input')?.focus();
    }
}

async function setView(view) {
    currentView = view;
    selectedIndex = 0;
    const input = paletteEl.querySelector('.cmdk-input');
    input.value = '';
    if (view === 'open-session') await loadSessions();
    if (view === 'new-session' || view === 'worktree') await loadProjects();
    if (view === 'root') await loadCustomCommands();
    renderView();
    focusActiveInput();
}

function goBack() {
    if (currentView === 'root') {
        closeCommandPalette();
        return;
    }
    prefillProject = '';
    currentCustomItem = null;
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
    browserStt.stop();  // end any in-flight dictation
    paletteEl.remove();
    paletteEl = null;
    currentView = 'root';
    currentItems = [];
    selectedIndex = 0;
    prefillProject = '';
    currentCustomItem = null;
    if (lastFocus && typeof lastFocus.focus === 'function') {
        try { lastFocus.focus(); } catch (e) { /* ignore */ }
    }
    lastFocus = null;
}

export function isCommandPaletteOpen() {
    return paletteEl !== null;
}
