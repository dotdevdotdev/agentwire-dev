// Drives the real TopologyView (topology-render.js) against a permissive stub
// DOM and prints one JSON object of results. Run by tests/unit/test_topology_expand_js.py.
// sessions-section.js (network/DOM heavy) is swapped for a stub via a resolve hook.
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const jsDir = process.argv[2];

register('data:text/javascript,' + encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  if (spec.endsWith('sidebar/sessions-section.js')) {
    return { url: 'data:text/javascript,export const activityStates = new Map();', shortCircuit: true };
  }
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
        this._cls = set; this.hidden = false; this.textContent = ''; this.title = '';
    }
    set className(v) { this._cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this._cls.add(c)); }
    get className() { return [...this._cls].join(' '); }
    set innerHTML(v) { this.children = []; }
    appendChild(c) { c.parentElement?.removeChild?.(c); c.parentElement = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach((c) => this.appendChild(c)); }
    insertBefore(c, ref) { this.appendChild(c); }
    before() {} after() {}
    removeChild(c) { this.children = this.children.filter((x) => x !== c); }
    remove() { this.parentElement?.removeChild(this); this.parentElement = null; }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    removeEventListener() {}
    setAttribute() {} getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; }
    closest(sel) { for (let n = this; n; n = n.parentElement) if (sel.split(',').some((s) => n._cls.has(s.trim().replace(/^\./, '')))) return n; return null; }
    querySelector() { return null; } querySelectorAll() { return []; }
    contains(o) { for (let n = o; n; n = n.parentElement) if (n === this) return true; return false; }
    focus() {} blur() {}
    fire(type, props = {}) {
        const e = { type, target: this, preventDefault() { this.prevented = true; }, stopPropagation() {}, ...props };
        (this.listeners[type] || []).forEach((fn) => fn(e));
        return e;
    }
}
globalThis.document = { createElement: (t) => new El(t), createElementNS: (_, t) => new El(t), addEventListener() {}, removeEventListener() {} };
globalThis.window = { addEventListener() {}, removeEventListener() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};

const { TopologyView } = await import(pathToFileURL(path.join(jsDir, 'topology-render.js')));

const live = new Set();
const disposed = [];
const mk = (n) => new TopologyView(new El('div'), {
    mode: 'shade',
    onCardExpand: (name) => { live.add(name); return () => { live.delete(name); disposed.push(name); }; },
});
const sess = (n) => [{ name: 'root', state: 'idle' },
    ...Array.from({ length: n }, (_, i) => ({ name: `c${i}`, parent: 'root', state: 'idle' })),
    { name: 'ghost', parent: 'root', state: 'orphan' }];
const out = {};

// Expanding one child must not close another (the accordion is gone).
let v = mk(); v.setSelfSession('root'); v.render(sess(3));
v._expandCard('c0'); v._expandCard('c1');
out.two_open_at_once = live.has('c0') && live.has('c1') && !disposed.length;

// expandAll opens every live child, never the self card or a ghost.
v.collapseAll();
const r = v.expandAll();
out.expand_all_scope = [...live].sort().join() === 'c0,c1,c2' && r.skipped === 0;
out.all_expanded_true = v.allExpanded() === true;
v.collapseAll();
out.collapse_all_disposes = live.size === 0 && v.allExpanded() === false;

// Cap: over the cap stays collapsed and is reported.
v = mk(); v.setSelfSession('root'); v.render(sess(10));
live.clear();
const capped = v.expandAll(8);
out.cap_respected = live.size === 8 && capped.skipped === 2 && !v.allExpanded();

// Keyboard: Enter on the card toggles; a key bubbling from inside does not.
v = mk(); v.setSelfSession('root'); v.render(sess(2)); live.clear();
const card = v._cards.get('c0').card;
card.fire('keydown', { key: 'Enter' });
out.enter_expands = live.has('c0');
card.fire('keydown', { key: 'Enter', target: new El('textarea') });
out.inner_key_ignored = live.has('c0');
card.fire('keydown', { key: ' ' });
out.space_collapses = !live.has('c0');

// dispose tears every terminal down.
v.expandAll(); v.dispose();
out.dispose_clears = live.size === 0;

console.log(JSON.stringify(out));
