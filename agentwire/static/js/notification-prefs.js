/**
 * Desktop notification preferences — frontend-only, per-browser.
 *
 * Two independent layers:
 *  - Browser permission ('default' | 'granted' | 'denied') — owned by the
 *    browser, requested via enableNotifications(). The portal can ask but
 *    cannot grant it; that's a per-origin browser decision.
 *  - A local "muted" toggle — lets the user silence bubbles without revoking
 *    the OS permission. Persisted in localStorage.
 *
 * Changing either dispatches `notification-pref-change` on window so the
 * Config sidebar (and anything else) can repaint.
 */

const MUTE_KEY = 'notificationsMuted';
export const NOTIFICATION_PREF_EVENT = 'notification-pref-change';

export function notificationsSupported() {
    return typeof window !== 'undefined' && 'Notification' in window;
}

/** 'unsupported' | 'default' | 'granted' | 'denied' */
export function getPermission() {
    return notificationsSupported() ? Notification.permission : 'unsupported';
}

export function isMuted() {
    return localStorage.getItem(MUTE_KEY) === '1';
}

export function setMuted(muted) {
    if (muted) localStorage.setItem(MUTE_KEY, '1');
    else localStorage.removeItem(MUTE_KEY);
    window.dispatchEvent(new CustomEvent(NOTIFICATION_PREF_EVENT));
}

/** True only when a bubble should actually be shown right now. */
export function notificationsActive() {
    return getPermission() === 'granted' && !isMuted();
}

/**
 * Ask the browser for permission if it hasn't been decided yet.
 * Returns the resulting permission string. No-op (returns current state) if
 * already granted/denied or unsupported.
 */
export async function enableNotifications() {
    if (!notificationsSupported()) return 'unsupported';
    let perm = Notification.permission;
    if (perm === 'default') {
        try { perm = await Notification.requestPermission(); }
        catch { perm = Notification.permission; }
    }
    window.dispatchEvent(new CustomEvent(NOTIFICATION_PREF_EVENT));
    return perm;
}
