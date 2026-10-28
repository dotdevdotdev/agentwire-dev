/**
 * Taskbar persistence gate — owns the "may we write the saved window set?" latch.
 *
 * Saves are frozen while a restore is replaying records (open* would otherwise
 * overwrite the saved set mid-replay) and while the page is going away.
 * Unload used to freeze on `beforeunload` and close every window, with nothing
 * ever thawing it. WebKit restores pages from the back/forward cache (`pageshow`
 * with `persisted`) and fires `beforeunload` for navigations that don't complete
 * (cancelled, download, mailto:), so the page came back with its windows closed
 * and saving permanently off. `pagehide` is the reliable lifecycle edge, and
 * its `pageshow` counterpart thaws.
 */

/** `getStorage` is a thunk: merely touching `localStorage` can throw in Safari. */
export function createTaskbarPersistence({ getStorage, key }) {
    let frozen = false;
    return {
        freeze() { frozen = true; },
        thaw() { frozen = false; },
        save(state) {
            if (frozen) return;
            try { getStorage().setItem(key, JSON.stringify(state)); } catch (e) {}
        },
        load() {
            const empty = { tabs: [], activeId: null };
            try {
                const raw = getStorage().getItem(key);
                if (!raw) return empty;
                const data = JSON.parse(raw);
                return {
                    tabs: Array.isArray(data.tabs) ? data.tabs : [],
                    activeId: data.activeId || null,
                };
            } catch (e) {
                return empty;
            }
        },
    };
}

/**
 * Freeze on pagehide, thaw on pageshow; `onLeave` runs only when the page is
 * really being discarded (not parked in the bfcache).
 */
export function bindPageLifecycle(win, persistence, { onLeave } = {}) {
    win.addEventListener('pagehide', (e) => {
        persistence.freeze();
        if (!e.persisted && onLeave) onLeave();
    });
    win.addEventListener('pageshow', (e) => {
        if (e.persisted) persistence.thaw();
    });
}
