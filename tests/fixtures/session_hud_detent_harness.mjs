// Drives the REAL SessionHud (session-hud.js) + HUD controller + TopologyView
// against a stub DOM and prints one JSON object of results. Run by
// tests/unit/test_session_hud_detent_js.py. Asserts the drawer RETURNS to its
// pre-expand peek height after every way of closing expanded cards (#1162).
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const jsDir = process.argv[2];
const stubs = {
    'hotkeys.js': 'export const hotkeys = globalThis.__hotkeys; export const bindHotkey = (k, fn) => { globalThis.__hotkeys.push([k, fn]); };',
    'sidebar.js': 'export const sidebar = { close() {} };',
    'scratchpad.js': 'export const scratchpad = { open: false, toggle() {} };',
    'sidebar/services-section.js': 'export const servicesSection = { mount() {}, refresh() {} };',
    'desktop-manager.js': 'export const desktop = { getActiveWindow: () => null, on() {} };',
    'sidebar/sessions-section.js': 'export const activityStates = new Map(); export const getAllSessions = () => globalThis.__sessions; export const onSessionsChanged = () => {}; export const ensureSessionsLoaded = () => {};',
    'card-terminal.js': 'export const mountCardTerminal = (name) => { globalThis.__live.add(name); return () => globalThis.__live.delete(name); }; export const mountSelfMic = () => {};',
    'api.js': 'export const apiFetch = async () => ({ ok: true, json: async () => ({}) });',
    'toast.js': 'export const toast = () => {}; export const toastSuccess = () => {}; export const toastError = () => {};',
    'safety-shared.js': 'export const projectFromCwd = () => "";',
};
globalThis.__hotkeys = [];
globalThis.__live = new Set();
globalThis.__sessions = [{ name: 'root', state: 'idle' },
    ...[0, 1].map((i) => ({ name: `c${i}`, parent: 'root', state: 'idle' }))];

register('data:text/javascript,' + encodeURIComponent(`
const stubs = ${JSON.stringify(stubs)};
export async function resolve(spec, ctx, next) {
  const key = Object.keys(stubs).find((k) => spec.endsWith('/' + k) || spec === './' + k);
  if (key) return { url: 'data:text/javascript,' + encodeURIComponent(stubs[key]), shortCircuit: true };
  return next(spec, ctx);
}`));

class El {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.parentElement = null; this.dataset = {};
        this.style = { setProperty() {}, removeProperty() {} }; this.listeners = {};
        const set = new Set();
        this.classList = {
            add: (...c) => c.forEach((x) => set.add(x)), remove: (...c) => c.forEach((x) => set.delete(x)),
            toggle: (c, f) => { const on = f ?? !set.has(c); on ? set.add(c) : set.delete(c); return on; },
            contains: (c) => set.has(c),
        };
        this._cls = set; this.hidden = false; this.textContent = ''; this.title = ''; this._named = {};
        this.scrollHeight = 0; this.offsetHeight = 0; this.clientHeight = 0;
    }
    set className(v) { this._cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this._cls.add(c)); }
    get className() { return [...this._cls].join(' '); }
    get firstElementChild() { return this.children[0] || null; }
    set innerHTML(v) {
        this.children = [];
        if (String(v).includes('session-hud-header')) {
            for (const c of ['session-hud-header', 'session-hud-notices', 'session-hud-canvas', 'session-hud-services']) {
                const e = new El('div'); e.className = c; this._named['.' + c] = e;
            }
        }
    }
    appendChild(c) { c.parentElement?.removeChild?.(c); c.parentElement = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach((c) => this.appendChild(c)); }
    insertBefore(c) { this.appendChild(c); }
    before() {} after() {}
    removeChild(c) { this.children = this.children.filter((x) => x !== c); }
    remove() { this.parentElement?.removeChild(this); this.parentElement = null; }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    removeEventListener() {}
    setAttribute() {}
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; }
    closest(sel) { for (let n = this; n; n = n.parentElement) if (sel.split(',').some((s) => n._cls.has(s.trim().replace(/^\./, '')))) return n; return null; }
    querySelector(sel) { return this._named[sel] || null; }
    querySelectorAll() { return []; }
    contains(o) { for (let n = o; n; n = n.parentElement) if (n === this) return true; return false; }
    focus() {} blur() {}
    fire(type, props = {}) {
        const e = { type, target: this, preventDefault() {}, stopPropagation() {}, ...props };
        (this.listeners[type] || []).forEach((fn) => fn(e));
        return e;
    }
}
const body = new El('body');
globalThis.document = {
    createElement: (t) => new El(t), createElementNS: (_, t) => new El(t),
    addEventListener() {}, removeEventListener() {}, body,
};
globalThis.window = { addEventListener() {}, removeEventListener() {}, innerHeight: 1000 };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.getComputedStyle = () => ({ paddingTop: '0', paddingBottom: '0', marginTop: '0', marginBottom: '0' });
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
let moCb = null;
globalThis.MutationObserver = class { constructor(cb) { moCb = cb; } observe() {} disconnect() {} };
let rafQ = [];
globalThis.requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
globalThis.cancelAnimationFrame = () => {};
const flush = () => { for (let i = 0; i < 5; i++) { moCb?.(); const q = rafQ; rafQ = []; q.forEach((f) => f()); } };

const { sessionHud } = await import(pathToFileURL(path.join(jsDir, 'session-hud.js')));
const { hudController } = await import(pathToFileURL(path.join(jsDir, 'session-hud-controller.js')));

sessionHud.init();
hudController.init(sessionHud.canvas, () => null);
// Content height grows with each live terminal, like the real expanded card does.
Object.defineProperty(sessionHud.canvas, 'scrollHeight', { get: () => 200 + 300 * globalThis.__live.size });
const press = (code, shift) => globalThis.__hotkeys.find(([k]) => k.code === code && !!k.shift === shift)[1]();
const view = hudController._view;
const settled = () => { flush(); return { detent: sessionHud.detent, half: sessionHud.drawer.classList.contains('detent-half'), h: sessionHud.drawer.style.height }; };

// Baseline: drawer open at peek, auto-sized to collapsed content.
sessionHud.toggle(true); hudController._render();
const base = settled();
const backToPeek = () => { const s = settled(); return s.detent === 'peek' && !s.half && s.h === base.h && base.h !== ''; };
const expandAll = () => { press('KeyP', true); flush(); };
const out = {};

expandAll();
out.expand_all_goes_half = sessionHud.detent === 'half' && globalThis.__live.size === view._expandableNames().length;
press('KeyP', true);
out.chord_collapse_all_returns_to_peek = backToPeek();

const names = view._expandableNames();
expandAll();
for (const n of names.slice(0, -1)) view.collapseCard(n);
out.half_held_until_last_closes = sessionHud.detent === 'half';
view.collapseCard(names.at(-1));
out.single_card_close_returns_to_peek = backToPeek();

expandAll();
names.forEach((n, i) => view._cards.get(n).card.fire('keydown', { key: i % 2 ? ' ' : 'Enter' }));
out.enter_space_close_returns_to_peek = backToPeek();

// Alt+P closes the drawer with cards still open; reopening + collapsing must land on peek.
expandAll(); press('KeyP', false); flush();
out.alt_p_closes = sessionHud.open === false;
press('KeyP', false); flush();
view.collapseAll();
out.alt_p_after_expand_all_returns_to_peek = backToPeek();

// Reopening after an Alt+P close shows collapsed cards at peek, not a stale tall drawer.
expandAll(); press('KeyP', false); flush(); press('KeyP', false);
out.alt_p_reopen_is_collapsed_peek = backToPeek() && globalThis.__live.size === 0;

// Drawer starts CLOSED: the chord opens it and expands; the second chord must restore peek.
press('KeyP', false); flush();
out.closed_start = sessionHud.open === false;
press('KeyP', true); flush();
press('KeyP', true);
out.closed_start_chord_returns_to_peek = backToPeek();
// Drawer closed while grown, then closed-start chord cycle again (detent must not have stuck at half).
expandAll(); press('KeyP', false); flush();
out.detent_not_stuck_half_after_alt_p_close = sessionHud.detent === 'peek';

console.log(JSON.stringify(out)); process.exit(0);
