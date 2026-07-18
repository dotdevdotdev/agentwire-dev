/**
 * session-hud-notices.js — pending artifact notices in the Session HUD (#817).
 *
 * Background-produced artifacts (council minutes, handoff renders, reports)
 * announce themselves as notifications instead of force-opening a window.
 * This module renders those pending notices as clickable cards in the
 * `.session-hud-notices` strip pinned above the HUD's segment canvases —
 * an ad-hoc entry type not tied to any live session/worktree card.
 *
 * Data source is notifications-panel.js's artifact-notice store (the SSOT
 * shared with the toasts), so the two surfaces always agree: dismissing or
 * clicking either one clears both.
 *
 * A card click dispatches the same `open-notification-artifact` event a
 * toast-body click does; desktop.js owns the single handler that dismisses
 * the notice, closes the HUD peek, and opens the artifact window. Event
 * rather than import because desktop.js is the module that boots this one —
 * a static import back would be circular (same reason as card-terminal.js).
 */

import { notificationsPanel } from './notifications-panel.js';

class HudNotices {
    constructor() {
        /** @type {HTMLElement|null} sessionHud's `.session-hud-notices` strip */
        this._container = null;
    }

    /** @param {HTMLElement} container - sessionHud.noticesEl */
    init(container) {
        this._container = container;
        notificationsPanel.onNoticesChanged(() => this._render());
        this._render();
    }

    // Full rebuild, not a diff: notices are few (bounded by active
    // notifications) and change rarely, unlike the topology's live tree.
    _render() {
        const notices = notificationsPanel.getArtifactNotices();
        this._container.replaceChildren(...notices.map((n) => this._buildCard(n)));
        this._container.hidden = notices.length === 0;
    }

    _buildCard(notice) {
        const artifact = notice.artifact || {};

        // A <div role="button">, not a <button>: the dismiss control nests
        // inside, and button-in-button is invalid HTML.
        const card = document.createElement('div');
        card.className = 'hud-notice-card';
        card.setAttribute('role', 'button');
        card.tabIndex = 0;
        card.title = 'Open artifact';

        const icon = document.createElement('span');
        icon.className = 'hud-notice-icon';
        icon.textContent = '⧉';
        icon.setAttribute('aria-hidden', 'true');

        const title = document.createElement('span');
        title.className = 'hud-notice-title';
        title.textContent = artifact.title || 'Artifact';

        const time = document.createElement('span');
        time.className = 'hud-notice-time';
        time.textContent = notice.timestamp
            ? new Date(notice.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';

        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'hud-notice-dismiss';
        dismiss.title = 'Dismiss';
        dismiss.innerHTML = '&times;';
        dismiss.addEventListener('click', (e) => {
            e.stopPropagation();
            notificationsPanel.dismiss(notice.id);
        });

        card.append(icon, title, time, dismiss);

        const open = () => {
            document.dispatchEvent(new CustomEvent('open-notification-artifact', {
                detail: {
                    url: artifact.url,
                    title: artifact.title,
                    artifactId: artifact.artifact_id,
                    noticeId: notice.id,
                },
            }));
        };
        card.addEventListener('click', (e) => {
            if (e.target.closest('.hud-notice-dismiss')) return;
            open();
        });
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                open();
            }
        });

        return card;
    }
}

export const hudNotices = new HudNotices();
