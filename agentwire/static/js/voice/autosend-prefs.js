/**
 * Push-to-talk auto-send preference — frontend-only, per-browser.
 *
 * When off (default), a final transcript drops into the edit-before-send bar
 * and the user confirms with Enter/➤. When on, the transcript is sent
 * immediately on release, skipping the bar.
 *
 * Persisted in localStorage so it survives reloads and is independent per
 * device (mobile vs desktop), which a single server config could not do.
 * Changing it dispatches `voice-autosend-change` on window so the Config
 * sidebar (and anything else) can repaint.
 */

const AUTOSEND_KEY = 'aw-voice-autosend';
export const AUTOSEND_EVENT = 'voice-autosend-change';

export function isAutoSend() {
    return localStorage.getItem(AUTOSEND_KEY) === '1';
}

export function setAutoSend(on) {
    if (on) localStorage.setItem(AUTOSEND_KEY, '1');
    else localStorage.removeItem(AUTOSEND_KEY);
    window.dispatchEvent(new CustomEvent(AUTOSEND_EVENT));
}
