/**
 * Dead-key composition suppressor — armed by hotkeys.js for Alt/Option chords.
 *
 * macOS quirk: Option+<letter> combos are dead keys (Option+` starts a
 * grave-accent composition, Option+N a tilde) against the focused element —
 * an xterm <textarea>, a transcript <input>, the palette field — before any
 * keydown handler runs, and the composed character is delivered through the
 * composition/input path, which preventDefault on keydown cannot cancel.
 * Result: hotkeys "work" but leak a stray ˜ or ` into the active box.
 *
 * Fix: arm() right before acting. For a short window, composition and
 * composition-flavoured input events are swallowed in the CAPTURE phase on
 * window (xterm's listeners live on the textarea, so stopping propagation
 * here means the accent never reaches the PTY or any app listener), and the
 * focused editable's pre-chord value is restored once the browser commits
 * the composed character natively.
 *
 * Chrome only finalizes a pending dead-key composition on blur or on the
 * next keypress — which can be long after the suppression window closes.
 * arm() therefore force-finalizes by blurring the focused editable
 * (compositionend fires immediately, inside the window) and restoring focus
 * on the next frame.
 */

/** e.code values that are macOS Option dead keys (grave, acute, umlaut, circumflex, tilde). */
export const DEAD_KEY_CODES = new Set(['Backquote', 'KeyE', 'KeyI', 'KeyN', 'KeyU']);

// Input types that carry a text value and accept setSelectionRange.
const TEXT_INPUT_TYPES = new Set(['text', 'search', 'url', 'tel', 'password']);

let suppressUntil = 0;
let snapshot = null;  // { el, value, start, end } of the editable focused at arm time

function isEditable(el) {
    if (!el) return false;
    if (el.tagName === 'TEXTAREA') return true;
    if (el.tagName === 'INPUT') return TEXT_INPUT_TYPES.has(el.type);
    return el.isContentEditable === true;
}

function takeSnapshot(el) {
    if (!isEditable(el) || el.isContentEditable) return null;
    return { el, value: el.value, start: el.selectionStart, end: el.selectionEnd };
}

// The browser commits the composed char into the target's value natively;
// the owner never saw the composition, so the residue would linger (xterm
// only rewrites its helper textarea through its own composition handling and
// would skew its position bookkeeping on the next real IME composition).
// Put back exactly what was there before the chord.
function restoreSnapshot(target) {
    if (!snapshot || snapshot.el !== target) return;
    if (target.value !== snapshot.value) target.value = snapshot.value;
    try {
        target.setSelectionRange(snapshot.start, snapshot.end);
    } catch { /* selection API unavailable on this input type */ }
}

// What a macOS dead key leaves behind: the five Option accents and their ASCII
// look-alikes. The residue check below only fires when the value is EXACTLY the
// pre-chord value with one of these spliced in at the caret, so a real
// keystroke typed inside the window can never be mistaken for it.
const ACCENTS = ['`', '\u00b4', '\u00a8', '\u02c6', '\u02dc', '^', '~'];

function isAccentResidue(el) {
    const { value, start, end } = snapshot;
    return ACCENTS.some((ch) => el.value === value.slice(0, start) + ch + value.slice(end));
}

// The commit can land after compositionend, via an input event of any type, or
// not announce itself at all, so the cleanup is by RESULT rather than by event:
// whenever we look inside the window and the box holds the accent, take it out.
function scrubResidue() {
    if (!snapshot || performance.now() > suppressUntil) return;
    const el = snapshot.el;
    if (el.isConnected === false || !isAccentResidue(el)) return;
    restoreSnapshot(el);
}

function swallow(e) {
    e.preventDefault();
    e.stopImmediatePropagation();
}

for (const type of ['compositionstart', 'compositionupdate', 'compositionend']) {
    window.addEventListener(type, (e) => {
        if (performance.now() > suppressUntil) return;
        swallow(e);
        if (e.type === 'compositionend') restoreSnapshot(e.target);
    }, true);
}

// Chrome emits the composed character as input events too; frameworks that
// listen for `input` (and any transcript box mirroring its value) must not
// see them either.
for (const type of ['beforeinput', 'input']) {
    window.addEventListener(type, (e) => {
        if (performance.now() > suppressUntil) return;
        if (e.inputType !== 'insertCompositionText') {
            if (type === 'input') scrubResidue();
            return;
        }
        swallow(e);
        if (type === 'input') restoreSnapshot(e.target);
    }, true);
}

/**
 * Swallow composition events for the next `ms` milliseconds and force any
 * pending dead-key composition on the focused editable to finalize (and be
 * swallowed) right now.
 */
export function armDeadKeySuppressor(ms = 700) {
    suppressUntil = performance.now() + ms;
    const ae = document.activeElement;
    snapshot = takeSnapshot(ae);
    if (isEditable(ae)) {
        ae.blur();  // forces Chrome to commit the composition into the armed window
        requestAnimationFrame(() => {
            ae.focus();
            scrubResidue();
        });
        // Chrome can commit the pending accent well after the blur; look again
        // until the window closes.
        for (const delay of [30, 100, 250, ms]) setTimeout(scrubResidue, delay);
    }
}
