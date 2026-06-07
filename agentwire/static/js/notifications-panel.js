/**
 * Notifications Panel — floating toast notifications anchored to bottom-right.
 *
 * Listens for 'notification' events from desktop-manager, renders toasts,
 * supports dismiss and click-to-open-session.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';

const NOTIFICATIONS_SESSION = 'agentwire-notifications';
const MAX_TOASTS = 8;

class NotificationsPanel {
    constructor() {
        /** @type {Map<string, HTMLElement>} id -> toast element */
        this.toasts = new Map();
        this.container = null;
    }

    init() {
        this.container = document.createElement('div');
        this.container.className = 'notification-panel';
        document.body.appendChild(this.container);

        // Listen for notification events
        desktop.on('notification', (data) => this._addToast(data));
        desktop.on('notification_dismiss', ({ id }) => this._removeToast(id));

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
        const { id, text, session, priority, timestamp } = notification;
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

        const toast = document.createElement('div');
        toast.className = `notification-toast${priority === 'high' ? ' high' : ''}`;
        toast.dataset.id = id;

        const timeStr = this._formatTime(timestamp);

        toast.innerHTML = `
            <div class="notification-toast-header">
                ${session ? `<span class="notification-session-badge">${this._escapeHtml(session)}</span>` : ''}
                <span class="notification-time">${timeStr}</span>
                <button class="notification-dismiss" title="Dismiss">&times;</button>
            </div>
            <div class="notification-toast-body">${this._escapeHtml(text)}</div>
        `;

        // Click body -> open notifications session terminal
        toast.querySelector('.notification-toast-body').addEventListener('click', () => {
            // Import dynamically to avoid circular deps
            const event = new CustomEvent('open-notification-session');
            document.dispatchEvent(event);
        });

        // Dismiss button
        toast.querySelector('.notification-dismiss').addEventListener('click', (e) => {
            e.stopPropagation();
            this._dismissToast(id);
        });

        // Prepend (newest on top — CSS uses flex-direction: column-reverse)
        this.container.appendChild(toast);

        // Trigger slide-in animation
        requestAnimationFrame(() => toast.classList.add('visible'));

        this.toasts.set(id, toast);
    }

    _removeToast(id, animate = true) {
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
}

export const notificationsPanel = new NotificationsPanel();
