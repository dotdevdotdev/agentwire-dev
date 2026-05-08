/**
 * Terminal font size preference — frontend-only, persisted in localStorage.
 *
 * If the user has set an override, every open terminal uses it.
 * Otherwise we fall back to a responsive default (20px on narrow viewports, 16px elsewhere).
 *
 * Changing the value dispatches a `terminal-font-size-change` event on window
 * so all open SessionWindows can update live.
 */

const STORAGE_KEY = 'terminalFontSize';
const NARROW_VIEWPORT = '(max-width: 768px)';
export const FONT_SIZE_MIN = 10;
export const FONT_SIZE_MAX = 28;
export const FONT_SIZE_EVENT = 'terminal-font-size-change';

function responsiveDefault() {
    return window.matchMedia(NARROW_VIEWPORT).matches ? 20 : 16;
}

export function getOverride() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const n = parseInt(raw, 10);
    if (Number.isFinite(n) && n >= FONT_SIZE_MIN && n <= FONT_SIZE_MAX) return n;
    return null;
}

export function getTerminalFontSize() {
    return getOverride() ?? responsiveDefault();
}

export function setTerminalFontSize(size) {
    const n = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, parseInt(size, 10)));
    if (!Number.isFinite(n)) return;
    localStorage.setItem(STORAGE_KEY, String(n));
    window.dispatchEvent(new CustomEvent(FONT_SIZE_EVENT, { detail: { size: n } }));
}

export function clearTerminalFontSize() {
    localStorage.removeItem(STORAGE_KEY);
    window.dispatchEvent(new CustomEvent(FONT_SIZE_EVENT, { detail: { size: getTerminalFontSize() } }));
}
