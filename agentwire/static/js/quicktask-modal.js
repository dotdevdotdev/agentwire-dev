/**
 * Quicktask Modal — fast launcher for "pull-base, branch-off, worktree, session".
 *
 * Hotkey: Cmd/Ctrl+K. Opens a modal that takes a project (autocompleted from
 * /api/projects), a base branch (default main), and a new branch, then calls
 * /api/create with worktree:true, base, pull_first. On success, opens the new
 * session terminal window.
 */

const PILL_TYPES = ['feat', 'fix', 'chore', 'refactor', 'docs'];
const LS_LAST_PROJECT = 'quicktask:lastProject';
const LS_BASE_PREFIX = 'quicktask:base:';

let modalEl = null;
let lastFocus = null;
let projectsCache = null;

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function slugify(text) {
    return String(text)
        .toLowerCase()
        .normalize('NFKD').replace(/[̀-ͯ]/g, '')
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 64);
}

async function fetchProjects() {
    if (projectsCache) return projectsCache;
    try {
        const res = await fetch('/api/projects');
        const data = await res.json();
        const all = data.projects || [];
        projectsCache = all.filter((p) => !p.machine || p.machine === 'local');
    } catch (e) {
        projectsCache = [];
    }
    return projectsCache;
}

function renderModal(projects) {
    const lastProject = localStorage.getItem(LS_LAST_PROJECT) || '';
    const baseFor = (proj) => localStorage.getItem(LS_BASE_PREFIX + proj) || 'main';
    const optionsHtml = projects.map((p) => `<option value="${escapeHtml(p.name)}">`).join('');
    const pillsHtml = PILL_TYPES.map((t) => `<button type="button" class="quicktask-pill" data-prefix="${t}">${t}</button>`).join('');

    return `<div class="modal-overlay" id="quicktaskOverlay">
        <div class="modal quicktask-modal">
            <div class="modal-header">
                <h3>Quicktask</h3>
                <button class="modal-close" data-action="close" aria-label="Close">×</button>
            </div>
            <div class="modal-body">
                <div class="quicktask-error" data-error hidden></div>
                <div class="quicktask-progress" data-progress hidden></div>
                <form class="quicktask-form">
                    <label class="quicktask-field">
                        <span class="quicktask-label">Project</span>
                        <input type="text" name="project" list="quicktaskProjects" value="${escapeHtml(lastProject)}" autocomplete="off" required />
                        <datalist id="quicktaskProjects">${optionsHtml}</datalist>
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
                        <button type="button" class="quicktask-btn-cancel" data-action="close">Cancel</button>
                        <button type="submit" class="quicktask-btn-submit">Create + Open</button>
                    </div>
                </form>
            </div>
        </div>
    </div>`;
}

function bind(form) {
    const titleInput = form.querySelector('input[name="title"]');
    const branchInput = form.querySelector('input[name="branch"]');
    const projectInput = form.querySelector('input[name="project"]');
    const baseInput = form.querySelector('input[name="base"]');
    const pillButtons = form.querySelectorAll('.quicktask-pill');

    let userEditedBranch = false;
    branchInput.addEventListener('input', () => { userEditedBranch = true; });

    titleInput?.addEventListener('input', () => {
        if (userEditedBranch) return;
        const slug = slugify(titleInput.value);
        const current = branchInput.value;
        // Preserve a leading `<type>/` prefix the user clicked
        const prefixMatch = current.match(/^([a-z]+)\//);
        branchInput.value = prefixMatch ? `${prefixMatch[1]}/${slug}` : slug;
    });

    pillButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            const prefix = btn.dataset.prefix;
            const stripped = branchInput.value.replace(/^[a-z]+\//, '');
            branchInput.value = `${prefix}/${stripped}`;
            branchInput.focus();
        });
    });

    projectInput?.addEventListener('change', () => {
        const proj = projectInput.value.trim();
        if (proj) {
            const stored = localStorage.getItem(LS_BASE_PREFIX + proj);
            if (stored) baseInput.value = stored;
        }
    });
}

function showError(text) {
    if (!modalEl) return;
    const el = modalEl.querySelector('[data-error]');
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
    const prog = modalEl.querySelector('[data-progress]');
    if (prog) prog.hidden = true;
}

function showProgress(label) {
    if (!modalEl) return;
    const el = modalEl.querySelector('[data-progress]');
    if (!el) return;
    el.innerHTML = `
        <div class="quicktask-spinner" aria-hidden="true"></div>
        <div class="quicktask-progress-label">${escapeHtml(label)}</div>
    `;
    el.hidden = false;
    const errEl = modalEl.querySelector('[data-error]');
    if (errEl) errEl.hidden = true;
    const form = modalEl.querySelector('.quicktask-form');
    if (form) form.hidden = true;
}

async function handleSubmit(e) {
    e.preventDefault();
    const form = e.target.closest('.quicktask-form');
    if (!form) return;

    const project = form.querySelector('input[name="project"]').value.trim();
    const base = form.querySelector('input[name="base"]').value.trim() || 'main';
    const branch = form.querySelector('input[name="branch"]').value.trim();
    const pullFirst = form.querySelector('input[name="pull_first"]').checked;

    if (!project || !branch) {
        showError('Project and new branch are required.');
        return;
    }

    localStorage.setItem(LS_LAST_PROJECT, project);
    localStorage.setItem(LS_BASE_PREFIX + project, base);

    showProgress(pullFirst ? `Pulling ${base} and starting ${branch}…` : `Starting ${branch}…`);

    try {
        const res = await fetch('/api/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: project,
                worktree: true,
                branch,
                base,
                pull_first: pullFirst,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            const formEl = modalEl.querySelector('.quicktask-form');
            if (formEl) formEl.hidden = false;
            const prog = modalEl.querySelector('[data-progress]');
            if (prog) prog.hidden = true;
            showError(data.error || `Create failed (HTTP ${res.status})`);
            return;
        }
        const sessionName = data.session || data.name || `${project}/${branch}`;
        closeQuicktaskModal();
        const { openSessionTerminal } = await import('./desktop.js');
        openSessionTerminal(sessionName, 'terminal');
    } catch (err) {
        const formEl = modalEl.querySelector('.quicktask-form');
        if (formEl) formEl.hidden = false;
        const prog = modalEl.querySelector('[data-progress]');
        if (prog) prog.hidden = true;
        showError(err?.message || 'Network error');
    }
}

function attachListeners() {
    if (!modalEl) return;
    modalEl.addEventListener('click', (e) => {
        const action = e.target.closest('[data-action]')?.dataset.action;
        if (action === 'close') closeQuicktaskModal();
        // Click outside the .modal closes it
        if (e.target === modalEl) closeQuicktaskModal();
    });
    modalEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.stopPropagation();
            closeQuicktaskModal();
        }
    });
    const form = modalEl.querySelector('.quicktask-form');
    if (form) {
        form.addEventListener('submit', handleSubmit);
        bind(form);
    }
}

export async function openQuicktaskModal() {
    if (modalEl) return;
    lastFocus = document.activeElement;
    const projects = await fetchProjects();
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderModal(projects);
    modalEl = wrapper.firstElementChild;
    document.body.appendChild(modalEl);
    attachListeners();
    // Focus the first empty required field
    const projectInput = modalEl.querySelector('input[name="project"]');
    const branchInput = modalEl.querySelector('input[name="branch"]');
    if (projectInput && !projectInput.value) projectInput.focus();
    else if (branchInput) branchInput.focus();
}

export function closeQuicktaskModal() {
    if (!modalEl) return;
    modalEl.remove();
    modalEl = null;
    if (lastFocus && typeof lastFocus.focus === 'function') {
        try { lastFocus.focus(); } catch (e) {}
    }
    lastFocus = null;
}

export function isQuicktaskOpen() {
    return modalEl !== null;
}
