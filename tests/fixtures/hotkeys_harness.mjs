// Drives the real hotkeys.js + dead-key-suppressor.js against a stub DOM and
// prints one JSON object of results. Run by tests/unit/test_hotkeys_js.py.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const jsDir = process.argv[2];

// --- stub DOM: capture-phase window listeners honouring stopImmediatePropagation
const listeners = [];
let now = 1000;
globalThis.performance = { now: () => now };
globalThis.requestAnimationFrame = (fn) => fn();

const doc = { activeElement: null };
globalThis.document = doc;
globalThis.window = {
    addEventListener(type, fn) { listeners.push({ type, fn }); },
};

function dispatch(type, props = {}) {
    const e = {
        type, defaultPrevented: false, stopped: false, immediateStopped: false,
        repeat: false, altKey: false, shiftKey: false, metaKey: false, ctrlKey: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.stopped = true; },
        stopImmediatePropagation() { this.immediateStopped = true; this.stopped = true; },
        ...props,
    };
    for (const l of listeners) {
        if (l.type !== type) continue;
        l.fn(e);
        if (e.immediateStopped) break;
    }
    return e;
}

function makeInput(value) {
    const el = {
        tagName: 'INPUT', type: 'text', value,
        selectionStart: value.length, selectionEnd: value.length,
        blurred: 0, focused: 0,
        blur() { this.blurred++; }, focus() { this.focused++; },
        setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e; },
    };
    return el;
}

const { bindHotkey } = await import(pathToFileURL(path.join(jsDir, 'hotkeys.js')));
const out = {};

// 1. Alt+` claims the key once, ignores repeat and non-Alt/Cmd variants.
let fired = 0;
bindHotkey({ code: 'Backquote', alt: true }, () => { fired++; });
const down = dispatch('keydown', { code: 'Backquote', altKey: true });
out.claimed = down.defaultPrevented && down.stopped;
out.fired_once = fired === 1;
dispatch('keydown', { code: 'Backquote', altKey: true, repeat: true });
out.repeat_ignored = fired === 1;
const cmd = dispatch('keydown', { code: 'Backquote', altKey: true, metaKey: true });
out.cmd_variant_untouched = !cmd.defaultPrevented && fired === 1;
const bare = dispatch('keydown', { code: 'Backquote' });
out.bare_untouched = !bare.defaultPrevented && fired === 1;

// 2. Safari order: Option goes down, the composition puts the accent in the
// field, THEN the chord's keydown arrives. The chord must restore the value
// captured when Option went down, and the caret with it.
const input = makeInput('hello');
input.selectionStart = 2; input.selectionEnd = 2;
doc.activeElement = input;
dispatch('keydown', { code: 'AltLeft', altKey: true });
input.value = 'he`llo'; input.selectionStart = 3; input.selectionEnd = 3;   // the browser's native composition
dispatch('keydown', { code: 'Backquote', altKey: true });
out.accent_removed = input.value === 'hello' && input.selectionStart === 2;

// 3. A real character typed before the chord fires is never taken for residue.
const typedBefore = makeInput('abc');
typedBefore.selectionStart = 3; typedBefore.selectionEnd = 3;
doc.activeElement = typedBefore;
dispatch('keydown', { code: 'AltLeft', altKey: true });
typedBefore.value = 'abcx';
dispatch('keydown', { code: 'Backquote', altKey: true });
out.typed_char_survives = typedBefore.value === 'abcx';

// 3b. Every dead key's accent is recognised, not just the grave.
bindHotkey({ code: ['KeyE', 'KeyU', 'KeyI', 'KeyN'], alt: true }, () => {});
const accentsOk = ['\u00b4', '\u00a8', '\u02c6', '\u02dc'].every((ch, i) => {
    const el = makeInput('ab'); el.selectionStart = 1; el.selectionEnd = 1;
    doc.activeElement = el;
    dispatch('keydown', { code: 'AltRight', altKey: true });
    el.value = 'a' + ch + 'b';
    dispatch('keydown', { code: ['KeyE', 'KeyU', 'KeyI', 'KeyN'][i], altKey: true });
    return el.value === 'ab';
});
out.every_accent_removed = accentsOk;

// 3c. Nothing focused, or a non-field focused: a chord must not throw or touch anything.
doc.activeElement = null;
dispatch('keydown', { code: 'AltLeft', altKey: true });
let threw = false;
try { dispatch('keydown', { code: 'Backquote', altKey: true }); } catch { threw = true; }
out.no_focus_is_safe = !threw;

// 5. Non-dead chord (Alt+P) never blurs the focused box; `when` can decline.
const box = makeInput('x');
doc.activeElement = box;
let peeked = 0;
bindHotkey({ code: 'KeyP', alt: true }, () => { peeked++; });
dispatch('keydown', { code: 'KeyP', altKey: true });
out.non_dead_leaves_box = peeked === 1 && box.value === 'x';
let open = true;
bindHotkey({ code: 'F3', when: () => !open }, () => {});
out.when_false_leaves_key = !dispatch('keydown', { code: 'F3' }).defaultPrevented;
open = false;
out.when_true_claims_key = dispatch('keydown', { code: 'F3' }).defaultPrevented;

// 6. allowRepeat lets a held chord keep firing.
let cycled = 0;
bindHotkey({ code: 'BracketRight', alt: true, allowRepeat: true }, () => { cycled++; });
dispatch('keydown', { code: 'BracketRight', altKey: true });
dispatch('keydown', { code: 'BracketRight', altKey: true, repeat: true });
out.allow_repeat = cycled === 2;

// 7. shift: false / true split one physical key into two chords (Alt+P peek vs
// Alt+Shift+P peek-all) — each fires only for its own, and both claim the key.
let peek = 0, peekAll = 0;
bindHotkey({ code: 'KeyP', alt: true, shift: false }, () => { peek++; });
bindHotkey({ code: 'KeyP', alt: true, shift: true }, () => { peekAll++; });
const plain = dispatch('keydown', { code: 'KeyP', altKey: true });
out.shift_up_peeks_only = peek === 1 && peekAll === 0 && plain.defaultPrevented;
const shifted = dispatch('keydown', { code: 'KeyP', altKey: true, shiftKey: true });
out.shift_down_peeks_all_only = peek === 1 && peekAll === 1 && shifted.defaultPrevented;

console.log(JSON.stringify(out));
