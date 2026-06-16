/**
 * toast.js — shared action-feedback primitives for the portal.
 *
 * SSOT for two things every state-changing UI action needs:
 *   1. toast()           — a transient success/failure/info notification
 *   2. withButtonBusy()  — disable + busy-mark a trigger button while in flight
 *   3. errorFromResponse() — best-effort human reason from a failed fetch
 *
 * Sidebar sections used to `console.error` silent failures and leave the UI
 * unchanged, so a click looked like a no-op. Route every action through these
 * helpers instead: success toast only after the server confirms, failure toast
 * carrying the reason, button disabled for the duration.
 */

let _container = null;

function _ensureContainer() {
    if (_container && document.body.contains(_container)) return _container;
    _container = document.createElement('div');
    _container.className = 'toast-container';
    document.body.appendChild(_container);
    return _container;
}

function _dismiss(el) {
    if (!el || el._dismissing) return;
    el._dismissing = true;
    clearTimeout(el._dismissTimer);
    el.classList.remove('toast-visible');
    el.addEventListener('transitionend', () => el.remove(), { once: true });
    // Fallback removal if transitionend never fires (display:none, reduced motion).
    setTimeout(() => el.remove(), 400);
}

/**
 * Show a transient toast in the bottom-right stack. Click to dismiss early.
 *
 * @param {string} message
 * @param {Object} [opts]
 * @param {'success'|'error'|'info'} [opts.type='info']
 * @param {number} [opts.duration] - ms before auto-dismiss (errors linger longer)
 * @returns {HTMLElement}
 */
export function toast(message, opts = {}) {
    const type = opts.type || 'info';
    const duration = opts.duration ?? (type === 'error' ? 6000 : 3500);
    const container = _ensureContainer();

    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    el.textContent = message;
    el.addEventListener('click', () => _dismiss(el));

    container.appendChild(el);
    requestAnimationFrame(() => el.classList.add('toast-visible'));

    el._dismissTimer = setTimeout(() => _dismiss(el), duration);
    return el;
}

export const toastSuccess = (msg, opts) => toast(msg, { ...opts, type: 'success' });
export const toastError = (msg, opts) => toast(msg, { ...opts, type: 'error' });

/**
 * Run an async action with the trigger button disabled + `.is-busy` for the
 * duration; the button is always restored afterwards (even if the action
 * re-renders the list out from under it — a detached node is harmless).
 * Caller owns success/failure messaging based on the result.
 *
 * @template T
 * @param {HTMLButtonElement|null} btn
 * @param {() => Promise<T>} fn
 * @param {Object} [opts]
 * @param {string} [opts.busyText] - temporary textContent while in flight
 * @returns {Promise<T>}
 */
export async function withButtonBusy(btn, fn, opts = {}) {
    if (!btn) return fn();
    const prevDisabled = btn.disabled;
    const hadBusyText = opts.busyText != null;
    const prevText = hadBusyText ? btn.textContent : null;
    btn.disabled = true;
    btn.classList.add('is-busy');
    if (hadBusyText) btn.textContent = opts.busyText;
    try {
        return await fn();
    } finally {
        btn.disabled = prevDisabled;
        btn.classList.remove('is-busy');
        if (hadBusyText) btn.textContent = prevText;
    }
}

/**
 * Best-effort human-readable error from a failed fetch Response. Portal
 * endpoints return JSON `{error}` on failure; fall back to the HTTP status.
 *
 * @param {Response} res
 * @returns {Promise<string>}
 */
export async function errorFromResponse(res) {
    try {
        const data = await res.clone().json();
        if (data && data.error) return data.error;
    } catch {
        // body wasn't JSON
    }
    return `HTTP ${res.status}`;
}
