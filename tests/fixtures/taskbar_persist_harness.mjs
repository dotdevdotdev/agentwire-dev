// Drives the REAL taskbar-persist.js against a stub window + storage.
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const mod = await import(pathToFileURL(path.join(process.argv[2], 'taskbar-persist.js')).href);
const { createTaskbarPersistence, bindPageLifecycle } = mod;

function page(storage) {
    const handlers = {};
    const win = { addEventListener: (t, f) => { (handlers[t] ||= []).push(f); } };
    const fire = (t, persisted = false) => (handlers[t] || []).forEach(f => f({ persisted }));
    const p = createTaskbarPersistence({ getStorage: () => storage, key: 'k' });
    let left = 0;
    bindPageLifecycle(win, p, { onLeave: () => { left++; } });
    return { p, fire, left: () => left };
}
const mem = () => { const m = new Map(); return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, v) }; };
const results = {};

// bfcache round-trip: hide (persisted) then show — saving must work again.
{
    const st = mem(), pg = page(st);
    pg.p.save({ tabs: [{ id: 'a' }], activeId: 'a' });
    pg.fire('pagehide', true);
    pg.p.save({ tabs: [], activeId: null });          // teardown noise: must be ignored
    pg.fire('pageshow', true);
    pg.p.save({ tabs: [{ id: 'b' }], activeId: 'b' });
    results.bfcache_thaws = pg.p.load().activeId === 'b';
    results.bfcache_keeps_disconnect_off = pg.left() === 0;
}
// Real unload: saves after pagehide never clobber the saved set; onLeave runs.
{
    const st = mem(), pg = page(st);
    pg.p.save({ tabs: [{ id: 'a' }], activeId: 'a' });
    pg.fire('pagehide', false);
    pg.p.save({ tabs: [], activeId: null });
    results.unload_preserves = pg.p.load().tabs.length === 1;
    results.unload_leaves = pg.left() === 1;
}
// Storage that throws (Safari blocked storage) never throws out of save/load.
{
    const bad = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('SecurityError'); } };
    const p = createTaskbarPersistence({ getStorage: () => { throw new Error('SecurityError'); }, key: 'k' });
    const p2 = createTaskbarPersistence({ getStorage: () => bad, key: 'k' });
    let ok = true;
    try { p.save({}); p2.save({}); ok = p.load().tabs.length === 0 && p2.load().tabs.length === 0; } catch (e) { ok = false; }
    results.storage_errors_swallowed = ok;
}
console.log(JSON.stringify(results));
