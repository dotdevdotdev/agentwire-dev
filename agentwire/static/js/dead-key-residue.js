/**
 * Dead-key residue cleanup — called by hotkeys.js when an Option chord fires.
 *
 * macOS Option+<letter> chords are dead keys (Option+` grave, N tilde, E acute,
 * I circumflex, U umlaut). In Safari the browser starts the composition
 * BEFORE the chord's keydown reaches the page: the observed order is
 *
 *     keydown Alt → compositionstart → compositionupdate → input (the accent is
 *     now in the field's value) → keydown Backquote (isComposing, kc 229)
 *
 * so by the time a hotkey handler runs the accent is already in the focused
 * box, and preventDefault on that keydown is too late to stop it. (Measured in
 * Safari 26 with a probe page; the xterm helper textarea, a plain <input> and
 * the palette field all behave the same.)
 *
 * The fix is to remember the field's value when Option itself goes down —
 * strictly before any composition — and put it back when the chord fires.
 * Assigning .value ends the pending composition cleanly (compositionend fires
 * at once, the next typed key is an ordinary key, no `à`), so there is nothing
 * else to cancel.
 *
 * The restore only happens when the field now holds EXACTLY the pre-chord value
 * with one accent spliced in at the caret, so a real character typed in the
 * meantime can never be mistaken for residue.
 */

/** e.code values that are macOS Option dead keys. */
export const DEAD_KEY_CODES = new Set(['Backquote', 'KeyE', 'KeyI', 'KeyN', 'KeyU']);

// The accents the five dead keys produce, and their ASCII look-alikes.
const ACCENTS = ['`', '´', '¨', 'ˆ', '˜', '^', '~'];

// Input types that carry a text value and accept setSelectionRange.
const TEXT_INPUT_TYPES = new Set(['text', 'search', 'url', 'tel', 'password']);

let preChord = null;  // { el, value, start, end } captured when Option went down

function isTextField(el) {
    if (!el) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && TEXT_INPUT_TYPES.has(el.type);
}

window.addEventListener('keydown', (e) => {
    if (e.code !== 'AltLeft' && e.code !== 'AltRight') return;
    const el = document.activeElement;
    preChord = isTextField(el)
        ? { el, value: el.value, start: el.selectionStart, end: el.selectionEnd }
        : null;
}, true);

/** Undo the accent the browser already put in the focused field, if any. */
export function scrubDeadKeyResidue() {
    if (!preChord) return;
    const { el, value, start, end } = preChord;
    if (el !== document.activeElement) return;
    const holdsResidue = ACCENTS.some((ch) => el.value === value.slice(0, start) + ch + value.slice(end));
    if (!holdsResidue) return;
    el.value = value;
    try {
        el.setSelectionRange(start, end);
    } catch { /* selection API unavailable on this input type */ }
}
