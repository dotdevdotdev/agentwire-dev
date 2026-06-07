/**
 * New Project Modal — create a fresh project under projects.dir.
 *
 * Submit calls /api/projects/create. Optional fields:
 *   - Clone URL (overrides empty-dir creation; runs `git clone` on the server)
 *   - "Initialize empty git repo" checkbox (ignored when a clone URL is set)
 */

import { apiFetch } from './api.js';

let modalEl = null;
let lastFocus = null;
let onCreatedCb = null;

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function deriveNameFromUrl(url) {
    const m = String(url).trim().match(/\/([^/]+?)(?:\.git)?\/?$/);
    return m ? m[1] : '';
}

function renderModal() {
    return `<div class="modal-overlay" id="newProjectOverlay">
        <div class="modal quicktask-modal">
            <div class="modal-header">
                <h3>New Project</h3>
                <button class="modal-close" data-action="close" aria-label="Close">×</button>
            </div>
            <div class="modal-body">
                <div class="quicktask-error" data-error hidden></div>
                <div class="quicktask-progress" data-progress hidden></div>
                <form class="quicktask-form">
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
                        <button type="button" class="quicktask-btn-cancel" data-action="close">Cancel</button>
                        <button type="submit" class="quicktask-btn-submit">Create</button>
                    </div>
                </form>
            </div>
        </div>
    </div>`;
}

function showError(text) {
    if (!modalEl) return;
    const el = modalEl.querySelector('[data-error]');
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
    const prog = modalEl.querySelector('[data-progress]');
    if (prog) prog.hidden = true;
    const form = modalEl.querySelector('.quicktask-form');
    if (form) form.hidden = false;
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

    const name = form.querySelector('input[name="name"]').value.trim();
    const cloneUrl = form.querySelector('input[name="clone_url"]').value.trim();
    const gitInit = form.querySelector('input[name="git_init"]').checked;

    if (!name) {
        showError('Project name is required.');
        return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
        showError("Invalid name (allowed: letters, digits, '.', '_', '-').");
        return;
    }

    showProgress(cloneUrl ? `Cloning ${cloneUrl}…` : `Creating ${name}…`);

    try {
        const res = await apiFetch('/api/projects/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                clone_url: cloneUrl || undefined,
                git_init: gitInit,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
            showError(data.error || `Create failed (HTTP ${res.status})`);
            return;
        }
        const cb = onCreatedCb;
        closeNewProjectModal();
        if (typeof cb === 'function') {
            try { cb(data); } catch (e) { console.warn('onCreated callback failed', e); }
        }
    } catch (err) {
        showError(err?.message || 'Network error');
    }
}

function attachListeners() {
    if (!modalEl) return;
    modalEl.addEventListener('click', (e) => {
        const action = e.target.closest('[data-action]')?.dataset.action;
        if (action === 'close') closeNewProjectModal();
        if (e.target === modalEl) closeNewProjectModal();
    });
    modalEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.stopPropagation();
            closeNewProjectModal();
        }
    });
    const form = modalEl.querySelector('.quicktask-form');
    if (!form) return;
    form.addEventListener('submit', handleSubmit);

    // Auto-fill name from clone URL
    const nameInput = form.querySelector('input[name="name"]');
    const urlInput = form.querySelector('input[name="clone_url"]');
    let userEditedName = false;
    nameInput?.addEventListener('input', () => { userEditedName = true; });
    urlInput?.addEventListener('input', () => {
        if (userEditedName) return;
        const derived = deriveNameFromUrl(urlInput.value);
        if (derived) nameInput.value = derived;
    });
}

export function openNewProjectModal({ onCreated } = {}) {
    if (modalEl) return;
    lastFocus = document.activeElement;
    onCreatedCb = onCreated || null;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderModal();
    modalEl = wrapper.firstElementChild;
    document.body.appendChild(modalEl);
    attachListeners();
    modalEl.querySelector('input[name="name"]')?.focus();
}

export function closeNewProjectModal() {
    if (!modalEl) return;
    modalEl.remove();
    modalEl = null;
    onCreatedCb = null;
    if (lastFocus && typeof lastFocus.focus === 'function') {
        try { lastFocus.focus(); } catch (e) {}
    }
    lastFocus = null;
}
