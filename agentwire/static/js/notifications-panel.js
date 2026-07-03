/**
 * Notifications Panel — floating toast notifications anchored to bottom-right.
 *
 * Listens for 'notification' events from desktop-manager, renders toasts,
 * supports dismiss and click-to-open-session.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';

const MAX_TOASTS = 8;
// Lifecycle contract: `normal` toasts are transient info — they auto-fade
// after this default (server may override per-toast via `timeout` seconds;
// 0 = sticky). `high` toasts are actionable and stick until dismissed.
const AUTO_FADE_SECONDS = 8;

class NotificationsPanel {
    constructor() {
        /** @type {Map<string, HTMLElement>} id -> toast element */
        this.toasts = new Map();
        /** @type {Map<string, number>} id -> auto-fade setTimeout handle */
        this.fadeTimers = new Map();
        this.container = null;
    }

    init() {
        this.container = document.createElement('div');
        this.container.className = 'notification-panel';
        document.body.appendChild(this.container);

        // Listen for notification events
        desktop.on('notification', (data) => this._addToast(data));
        desktop.on('notification_dismiss', ({ id }) => this._removeToast(id));

        // Auto-fade only counts down while the tab is actually visible —
        // a toast posted to a background tab shouldn't vanish unseen.
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                for (const [id, toast] of this.toasts) this._scheduleFade(id, toast);
            } else {
                for (const id of this.fadeTimers.keys()) this._cancelFade(id);
            }
        });

        // Restore active notifications on page load
        this._restore();
    }

    async _restore() {
        try {
            const resp = await apiFetch('/api/desktop/notifications');
            if (!resp.ok) return;
            const data = await resp.json();
            const notifications = data.notifications || [];
            for (const n of notifications) {
                this._addToast(n);
            }
        } catch {
            // Portal not reachable, ignore
        }
    }

    _addToast(notification) {
        const { id, text, session, priority, timestamp, timeout } = notification;
        if (!id || !text) return;

        // Update existing toast if same id
        if (this.toasts.has(id)) {
            this._removeToast(id, false);
        }

        // Evict oldest if at capacity
        if (this.toasts.size >= MAX_TOASTS) {
            const oldest = this.toasts.keys().next().value;
            this._removeToast(oldest, false);
        }

        // Resolve auto-fade: explicit timeout wins (0 = sticky); otherwise
        // high priority sticks and normal fades after the default.
        let fadeSeconds = 0;
        if (timeout !== undefined && timeout !== null) {
            fadeSeconds = Number(timeout) || 0;
        } else if (priority !== 'high') {
            fadeSeconds = AUTO_FADE_SECONDS;
        }

        const toast = document.createElement('div');
        toast.className = `notification-toast${priority === 'high' ? ' high' : ''}${fadeSeconds ? ' auto-fade' : ''}`;
        toast.dataset.id = id;
        toast.dataset.session = session || '';
        toast.dataset.fadeSeconds = String(fadeSeconds);

        const timeStr = this._formatTime(timestamp);

        toast.innerHTML = `
            <div class="notification-toast-header">
                ${session ? `<span class="notification-session-badge">${this._escapeHtml(session)}</span>` : ''}
                <span class="notification-time">${timeStr}</span>
                <button class="notification-dismiss" title="Dismiss">&times;</button>
            </div>
            <div class="notification-toast-body">${this._renderRichText(text)}</div>
        `;

        // Click body -> open the subject session this notification is about
        toast.querySelector('.notification-toast-body').addEventListener('click', () => {
            const event = new CustomEvent('open-notification-session', {
                detail: { session: toast.dataset.session || '' },
            });
            document.dispatchEvent(event);
        });

        // Dismiss button
        toast.querySelector('.notification-dismiss').addEventListener('click', (e) => {
            e.stopPropagation();
            this._dismissToast(id);
        });

        // Hovering pauses the auto-fade — the user is reading it.
        toast.addEventListener('mouseenter', () => this._cancelFade(id));
        toast.addEventListener('mouseleave', () => this._scheduleFade(id, toast));

        // Prepend (newest on top — CSS uses flex-direction: column-reverse)
        this.container.appendChild(toast);

        // Trigger slide-in animation
        requestAnimationFrame(() => toast.classList.add('visible'));

        this.toasts.set(id, toast);
        this._scheduleFade(id, toast);
    }

    /**
     * (Re)start the auto-fade countdown for a transient toast. No-op for
     * sticky toasts (fadeSeconds 0) or while the tab is hidden. Restarts the
     * full duration on resume/unhover — simple beats precise here. Fading
     * routes through _dismissToast: the toast sat on a visible dashboard for
     * its whole countdown, so the dismissal is persisted server-side.
     */
    _scheduleFade(id, toast) {
        const seconds = Number(toast.dataset.fadeSeconds) || 0;
        if (!seconds || document.visibilityState !== 'visible') return;
        this._cancelFade(id);
        this.fadeTimers.set(id, setTimeout(() => this._dismissToast(id), seconds * 1000));
        // Restart the fade-progress cue in sync with the timer.
        toast.style.setProperty('--fade-duration', `${seconds}s`);
        toast.classList.remove('fading');
        requestAnimationFrame(() => toast.classList.add('fading'));
    }

    _cancelFade(id) {
        const timer = this.fadeTimers.get(id);
        if (timer !== undefined) {
            clearTimeout(timer);
            this.fadeTimers.delete(id);
        }
        const toast = this.toasts.get(id);
        if (toast) toast.classList.remove('fading');
    }

    _removeToast(id, animate = true) {
        this._cancelFade(id);
        const toast = this.toasts.get(id);
        if (!toast) return;

        if (animate) {
            toast.classList.add('dismissing');
            toast.addEventListener('animationend', () => toast.remove(), { once: true });
        } else {
            toast.remove();
        }
        this.toasts.delete(id);
    }

    /**
     * Auto-dismiss every outstanding toast tied to a given session — called
     * when the user tabs into that session's window, so a notification they've
     * clearly seen stops being noise. Toasts for other sessions are untouched.
     * Routes through _dismissToast so the dismissal is persisted server-side and
     * won't reappear on reload.
     *
     * @param {string} session
     */
    dismissForSession(session) {
        if (!session) return;
        for (const [id, toast] of this.toasts) {
            if (toast.dataset.session === session) {
                this._dismissToast(id);
            }
        }
    }

    async _dismissToast(id) {
        this._removeToast(id, true);
        try {
            await apiFetch('/api/desktop/notification/dismiss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id }),
            });
        } catch {
            // Best effort
        }
    }

    _formatTime(timestamp) {
        if (!timestamp) return '';
        const d = new Date(timestamp * 1000);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    _escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Render a SAFE markdown subset: bold, links, line breaks. Escape everything
    // FIRST so no source HTML survives, then introduce only our own known tags.
    // Links are restricted to http(s)/mailto (no javascript:/data:), and because
    // quotes are already escaped, the agent text can't break out of the href.
    _renderRichText(str) {
        let s = this._escapeHtml(str);
        // Links before bold so [**label**](url) composes. URL came through escape,
        // so any " is already &quot; — it can't close the attribute.
        s = s.replace(
            /\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
            (_m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
        );
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/\n/g, '<br>');
        return s;
    }
}

export const notificationsPanel = new NotificationsPanel();
