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
const timeouts = [];
globalThis.setTimeout = (fn) => { timeouts.push(fn); return timeouts.length; };
const runTimeouts = () => timeouts.splice(0).forEach((fn) => fn());
const doc = { activeElement: null };
globalThis.document = doc;
globalThis.window = {
    addEventListener(type, fn) { listeners.push({ type, fn }); },
};

function dispatch(type, props = {}) {
    const e = {
        type, defaultPrevented: false, stopped: false, immediateStopped: false,
        repeat: false, altKey: false, metaKey: false, ctrlKey: false,
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

// 2. A dead-key chord scrubs the accent out of a visible (non-xterm) input.
const input = makeInput('hello');
input.selectionStart = 2; input.selectionEnd = 2;
doc.activeElement = input;
dispatch('keydown', { code: 'Backquote', altKey: true });  // arms + snapshots + blurs
out.blurred_for_commit = input.blurred === 1 && input.focused === 1;
const seen = [];
listeners.push({ type: 'input', fn: (e) => seen.push(e.type) });  // an app listener, registered after
listeners.push({ type: 'compositionend', fn: (e) => seen.push(e.type) });
input.value = 'he`llo';  // the browser's native commit of the composed char
dispatch('compositionstart', { target: input });
dispatch('beforeinput', { target: input, inputType: 'insertCompositionText' });
dispatch('input', { target: input, inputType: 'insertCompositionText' });
dispatch('compositionend', { target: input });
out.value_restored = input.value === 'hello' && input.selectionStart === 2;
out.app_listeners_never_saw_it = seen.length === 0;

// 3. Real typing (not composition) during the window is left alone.
input.value = 'hellox';
const typed = dispatch('input', { target: input, inputType: 'insertText' });
out.plain_typing_untouched = !typed.immediateStopped && input.value === 'hellox';

// 3b. A commit that lands LATE and announces itself with no composition event
// (the leak the event-based cleanup missed) is scrubbed by the timed re-check.
const late1 = makeInput('abc');
late1.selectionStart = 3; late1.selectionEnd = 3;
doc.activeElement = late1;
timeouts.length = 0;
dispatch('keydown', { code: 'Backquote', altKey: true });
late1.value = 'abc`';  // native commit, no events dispatched
runTimeouts();
out.silent_late_commit_scrubbed = late1.value === 'abc';

// 3c. ...but a real character typed inside the window is never taken for it.
const late2 = makeInput('abc');
late2.selectionStart = 3; late2.selectionEnd = 3;
doc.activeElement = late2;
dispatch('keydown', { code: 'Backquote', altKey: true });
late2.value = 'abcx';
runTimeouts();
out.typed_char_survives_recheck = late2.value === 'abcx';

// 4. Outside the window nothing is swallowed.
now += 5000;
const late = dispatch('compositionend', { target: input });
out.expired_passes_through = !late.defaultPrevented;

// 5. Non-dead chord (Alt+P) never blurs the focused box; `when` can decline.
const box = makeInput('x');
doc.activeElement = box;
let peeked = 0;
bindHotkey({ code: 'KeyP', alt: true }, () => { peeked++; });
dispatch('keydown', { code: 'KeyP', altKey: true });
out.non_dead_no_blur = peeked === 1 && box.blurred === 0;
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

console.log(JSON.stringify(out));
