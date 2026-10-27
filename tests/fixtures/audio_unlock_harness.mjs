// Drives the REAL desktop-manager.js + voice/audio-unlock.js against a stub
// AudioContext that behaves like Safari: suspended, and resume() only settles
// when called inside an accepted gesture (pointerdown/touchend/click — never
// keydown). Prints one JSON object of results. Run by tests/unit/test_audio_unlock_js.py.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const jsDir = process.argv[2];

const listeners = [];
let inGesture = false;
const started = [];
const els = [];

globalThis.window = {
    addEventListener(type, fn) { listeners.push({ type, fn }); },
};
globalThis.requestAnimationFrame = (fn) => fn();
globalThis.document = {
    body: { appendChild(el) { els.push(el); el._in = true; }, contains: (el) => !!el._in },
    createElement() {
        return {
            className: '',
            appendChild(child) { child._in = true; els.push(child); },
            classList: { add() {}, remove() {} },
            setAttribute() {},
            addEventListener(t, fn) { if (t === 'click') this._click = fn; },
            click() { this._click?.(); },
            remove() { this._in = false; },
        };
    },
};

class StubAudioContext {
    constructor() { this.state = 'suspended'; this.destination = {}; }
    resume() {
        if (!inGesture) return new Promise(() => {}); // Safari: pends forever
        this.state = 'running';
        return Promise.resolve();
    }
    createBuffer() { return { silent: true }; }
    createBufferSource() {
        const s = { buffer: null, connect() {}, start: () => { started.push(s); setTimeout(() => s.onended?.(), 0); } };
        return s;
    }
    async decodeAudioData() { return { decoded: true }; }
}
globalThis.window.AudioContext = StubAudioContext;

function gesture(type) {
    inGesture = !['keydown'].includes(type);
    for (const l of listeners) if (l.type === type) l.fn({ type });
    inGesture = false;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const audioStarts = () => started.filter((s) => s.buffer && s.buffer.decoded).length;
const silentStarts = () => started.filter((s) => s.buffer && s.buffer.silent).length;

const { desktop } = await import(pathToFileURL(path.join(jsDir, 'desktop-manager.js')).href);
globalThis.WebSocket = class { constructor() { throw new Error('no ws'); } };
globalThis.location = { protocol: 'http:', host: 'x' };
globalThis.localStorage = { getItem: () => null };
await desktop.connect().catch(() => {});
desktop.reconnectTimeout && clearTimeout(desktop.reconnectTimeout);
desktop.intentionalDisconnect = true;

const live = () => els.filter((e) => e._in && e.className.startsWith('toast ')).length;
const b64 = (n) => Buffer.from(`wav-${n}`.padEnd(80, 'x')).toString('base64');
const r = {};

// 1. audio before any gesture: queued, not dropped, not played; user is told
await desktop._playAudio(b64(1), 's');
await tick();
r.queued_not_dropped = desktop._audioQueue.length === 1;
r.not_played_while_locked = audioStarts() === 0;
r.unlock_prompt_shown = live() === 1;

// 2. keydown alone is not an unlock
gesture('keydown');
await tick();
r.keydown_does_not_unlock = audioStarts() === 0 && desktop._audioQueue.length === 1;

// 3. first pointerdown unlocks (with silent sample) and flushes the queue
gesture('pointerdown');
await tick(); await tick();
r.pointerdown_flushes_queue = audioStarts() === 1 && desktop._audioQueue.length === 0;
r.silent_buffer_played = silentStarts() >= 1;

// 4. later audio plays immediately, no gesture, no new prompt
await new Promise((res) => setTimeout(res, 450)); // inter-clip gap + toast removal
r.prompt_dismissed_on_unlock = live() === 0;
await desktop._playAudio(b64(2), 's');
await tick(); await tick();
r.second_audio_plays_immediately = audioStarts() === 2;
r.no_second_prompt = els.filter((e) => e.className.startsWith('toast ')).length === 1;

console.log(JSON.stringify(r));
process.exit(0);
