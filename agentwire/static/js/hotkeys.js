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

const MOD_CODES = new Set(['ControlLeft', 'ControlRight', 'MetaLeft', 'MetaRight']);

/**
 * @param {object} spec
 * @param {string|string[]} spec.code    e.code value(s) to claim
 * @param {boolean} [spec.alt=false]     require Option/Alt (and no Cmd/Ctrl); when false, modifiers are ignored
 * @param {boolean} [spec.mod=false]     require Ctrl or Cmd (and no Option); don't combine with `alt`
 * @param {boolean|null} [spec.shift=null]  true = require Shift, false = require Shift up, null = ignore Shift
 * @param {() => boolean} [spec.when]    return false to leave the key alone (e.g. a modal owns the keyboard)
 * @param {boolean} [spec.allowRepeat=false]  deliver auto-repeat keydowns to the handler
 * @param {() => void} [spec.onRelease]  hold-to-talk: called once when a claimed chord is let go — the
 *                                       key's keyup, or the modifier's (macOS drops the key's keyup while Cmd is held)
 * @param {(e: KeyboardEvent) => void} handler
 * @returns {() => void} unbind
 */
export function bindHotkey({ code, alt = false, mod = false, shift = null, when = null, allowRepeat = false, onRelease = null }, handler) {
    const codes = new Set(Array.isArray(code) ? code : [code]);
    let held = false;
    const onDown = (e) => {
        if (!codes.has(e.code)) return;
        if (alt && !(e.altKey && !e.metaKey && !e.ctrlKey)) return;
        if (mod && !((e.ctrlKey || e.metaKey) && !e.altKey)) return;
        if (shift !== null && e.shiftKey !== shift) return;
        if (when && !when()) return;
        e.preventDefault();
        e.stopPropagation();
        if (alt && DEAD_KEY_CODES.has(e.code)) scrubDeadKeyResidue();
        if (e.repeat && !allowRepeat) return;
        held = true;
        handler(e);
    };
    const onUp = (e) => {
        if (!held || !(codes.has(e.code) || MOD_CODES.has(e.code))) return;
        held = false;
        e.preventDefault();
        e.stopPropagation();
        onRelease();
    };
    window.addEventListener('keydown', onDown, true);
    if (onRelease) window.addEventListener('keyup', onUp, true);
    return () => {
        window.removeEventListener('keydown', onDown, true);
        window.removeEventListener('keyup', onUp, true);
    };
}
