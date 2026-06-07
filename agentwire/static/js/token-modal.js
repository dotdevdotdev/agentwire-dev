/**
 * Token entry modal — one-time per device auth for the portal.
 *
 * Shown by apiFetch on 401. The user pastes the token from
 * `agentwire portal token`; we verify it against the API, persist it via
 * api.js, then reload the page so every WebSocket and initial data load
 * reconnects with credentials.
 */

import { setToken } from './api.js';

let modalEl = null;

function renderModal() {
    return `<div class="modal-overlay" id="tokenModalOverlay">
        <div class="modal quicktask-modal">
            <div class="modal-header">
                <h3>Connect to AgentWire</h3>
            </div>
            <div class="modal-body">
                <div class="quicktask-error" data-error hidden></div>
                <form class="quicktask-form">
                    <label class="quicktask-field">
                        <span class="quicktask-label">Auth token</span>
                        <input type="password" name="token" placeholder="Paste your portal token" autocomplete="off" required />
                    </label>
                    <p class="quicktask-hint">Run <code>agentwire portal token</code> on the portal machine to see it. You only need to do this once per device.</p>
                    <div class="quicktask-footer">
                        <button type="submit" class="quicktask-btn-submit">Connect</button>
                    </div>
                </form>
            </div>
        </div>
    </div>`;
}

function showError(text) {
    const el = modalEl?.querySelector('[data-error]');
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
}

async function verifyToken(token) {
    // Raw fetch (not apiFetch) — a 401 here must not recurse into the modal.
    try {
        const resp = await fetch('/api/sessions/local', {
            headers: { 'Authorization': `Bearer ${token}` },
        });
        return resp.status !== 401;
    } catch {
        return false;
    }
}

/**
 * Show the modal; resolves once a valid token has been stored.
 * In practice the page reloads on success, so callers' retries are moot.
 */
export function showTokenModal() {
    return new Promise((resolve) => {
        if (modalEl) return; // already visible; existing promise governs
        const wrapper = document.createElement('div');
        wrapper.innerHTML = renderModal();
        modalEl = wrapper.firstElementChild;
        document.body.appendChild(modalEl);

        const form = modalEl.querySelector('.quicktask-form');
        const input = modalEl.querySelector('input[name="token"]');
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const token = input.value.trim();
            if (!token) return;
            const btn = form.querySelector('.quicktask-btn-submit');
            btn.disabled = true;
            btn.textContent = 'Connecting…';
            if (await verifyToken(token)) {
                setToken(token);
                modalEl.remove();
                modalEl = null;
                resolve();
                // Reconnect everything (WebSockets, initial loads) with auth.
                location.reload();
            } else {
                btn.disabled = false;
                btn.textContent = 'Connect';
                showError('Invalid token — check `agentwire portal token` and try again.');
            }
        });
        input.focus();
    });
}
