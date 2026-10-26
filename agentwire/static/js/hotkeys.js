/**
 * Global hotkey binding — the one place a portal hotkey claims its keystroke.
 *
 * A hotkey must never reach the focused element (an xterm <textarea>, a
 * transcript <input>, the palette field), or its character lands in the box.
 * Every claim therefore: runs in the CAPTURE phase on window, preventDefault +
 * stopPropagation, ignores auto-repeat unless asked, and — for the macOS dead
 * keys — removes the accent the browser has already put in the focused field
 * (see dead-key-residue.js). Binding through here means a new chord can't
 * forget any of that.
 *
 * Detection is by e.code (the physical key), never e.key: Option rewrites
 * e.key on macOS ('Dead', 'π', '“').
 */

import { scrubDeadKeyResidue, DEAD_KEY_CODES } from './dead-key-residue.js';

/**
 * @param {object} spec
 * @param {string|string[]} spec.code    e.code value(s) to claim
 * @param {boolean} [spec.alt=false]     require Option/Alt (and no Cmd/Ctrl); when false, modifiers are ignored
 * @param {boolean|null} [spec.shift=null]  true = require Shift, false = require Shift up, null = ignore Shift
 * @param {() => boolean} [spec.when]    return false to leave the key alone (e.g. a modal owns the keyboard)
 * @param {boolean} [spec.allowRepeat=false]  deliver auto-repeat keydowns to the handler
 * @param {(e: KeyboardEvent) => void} handler
 */
export function bindHotkey({ code, alt = false, shift = null, when = null, allowRepeat = false }, handler) {
    const codes = new Set(Array.isArray(code) ? code : [code]);
    window.addEventListener('keydown', (e) => {
        if (!codes.has(e.code)) return;
        if (alt && !(e.altKey && !e.metaKey && !e.ctrlKey)) return;
        if (shift !== null && e.shiftKey !== shift) return;
        if (when && !when()) return;
        e.preventDefault();
        e.stopPropagation();
        if (alt && DEAD_KEY_CODES.has(e.code)) scrubDeadKeyResidue();
        if (e.repeat && !allowRepeat) return;
        handler(e);
    }, true);
}
