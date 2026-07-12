/**
 * lineage.js
 *
 * Maps a session to its family's lineage-tint CSS variable (#749 SSOT —
 * `agentwire/static/css/desktop.css` `--lineage-tint-1..6`). A family is a
 * root session plus every descendant reachable by walking `.parent` links;
 * the whole family shares one hue so relatedness reads at a glance without
 * labels. Consumed by the born-from-parent ghost (#745), grouped collage
 * slices, and the shared topology renderer (#761) rather than each
 * re-deriving its own palette or walk.
 *
 * @module lineage
 */

const TINT_COUNT = 6;

/**
 * Walk `.parent` links from `name` to its root ancestor, tracking depth.
 * Cycle-safe via an exact `seen` set (a 2-cycle stops immediately rather than
 * spinning for a fixed number of iterations). The one shared implementation
 * of "resolve a session's family root" — `familyRootName` below and
 * `groupFamilies` both build on it, and `collage.js`'s window-id family
 * grouping calls it directly, so there is exactly one root-resolution walk
 * in the codebase, not a copy per consumer.
 *
 * @param {Map<string, {name?: string, parent?: string|null}>} byName - Session name → record.
 * @param {string} name - Session name to resolve.
 * @returns {{root: string, depth: number}} Root ancestor's name and hop count to it.
 */
export function lineageOf(byName, name) {
    let cur = name;
    let depth = 0;
    const seen = new Set([name]);
    while (true) {
        const parent = byName.get(cur)?.parent;
        if (!parent || parent === cur || !byName.has(parent) || seen.has(parent)) break;
        seen.add(parent);
        cur = parent;
        depth++;
    }
    return { root: cur, depth };
}

/**
 * Tolerant of missing sessions, self-referencing parents, and cycles.
 *
 * @param {string} name - Session name to resolve.
 * @param {Array<{name: string, parent?: string|null}>} sessions - Full session list.
 * @returns {string} The root ancestor's name (or `name` itself if it has no parent).
 */
export function familyRootName(name, sessions) {
    const byName = new Map((sessions || []).map((s) => [s.name, s]));
    return lineageOf(byName, name).root;
}

/**
 * Group a session list into families (a root + every descendant reachable
 * via `.parent`), each family's members ordered ancestor-first (root, then
 * descendants by increasing depth) so parent-before-child render/layout
 * order falls out for free. Shared by the topology renderer (#761) for its
 * card layout; `collage.js` groups *window ids* instead (some of which are
 * non-session windows with no lineage) so it calls `lineageOf` directly
 * rather than this session-list form.
 *
 * @param {Array<{name: string, parent?: string|null}>} sessions
 * @returns {Array<{root: string, members: Array<{name: string, depth: number}>}>}
 */
export function groupFamilies(sessions) {
    const byName = new Map((sessions || []).map((s) => [s.name, s]));
    const families = new Map(); // root name → [{name, depth}]
    for (const s of sessions || []) {
        const name = s.name;
        if (!name) continue;
        const { root, depth } = lineageOf(byName, name);
        if (!families.has(root)) families.set(root, []);
        families.get(root).push({ name, depth });
    }
    return [...families.entries()].map(([root, members]) => ({
        root,
        members: members.sort((a, b) => a.depth - b.depth),
    }));
}

/**
 * Build the subtree rooted at `name`: `name` itself plus every session
 * reachable by walking descendant `.parent` links from it (ancestors and
 * siblings excluded). Used by the Session HUD controller (#778) to re-root
 * the view onto a focused session — passing the result back into
 * `groupFamilies`/`TopologyView.render()` makes `name` resolve as the root
 * (its real parent, if any, isn't in the subset so `lineageOf` stops there)
 * without needing a second "root override" concept anywhere downstream.
 *
 * @param {string} name - Session name to re-root onto.
 * @param {Array<{name: string, parent?: string|null}>} sessions - Full session list.
 * @returns {Array<object>} The subset of `sessions` in the subtree, root first then descendants.
 */
export function subtreeOf(name, sessions) {
    const list = sessions || [];
    const byName = new Map(list.map((s) => [s.name, s]));
    if (!byName.has(name)) return [];

    const childrenOf = new Map();
    for (const s of list) {
        if (!s.name || !s.parent || s.parent === s.name) continue;
        if (!childrenOf.has(s.parent)) childrenOf.set(s.parent, []);
        childrenOf.get(s.parent).push(s.name);
    }

    const seen = new Set([name]);
    const order = [name];
    for (let i = 0; i < order.length; i++) {
        for (const child of childrenOf.get(order[i]) || []) {
            if (seen.has(child)) continue; // cycle-safe
            seen.add(child);
            order.push(child);
        }
    }
    return order.map((n) => byName.get(n)).filter(Boolean);
}

/** Deterministic small-int hash of a string, stable across reloads/renders. */
function hashIndex(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) {
        h = (h * 31 + str.charCodeAt(i)) | 0;
    }
    return Math.abs(h) % TINT_COUNT;
}

/**
 * @param {string} name - Session name.
 * @param {Array<{name: string, parent?: string|null}>} sessions - Full session list.
 * @returns {string} A `--lineage-tint-N` custom property name (1-indexed).
 */
export function lineageTintVar(name, sessions) {
    const root = familyRootName(name, sessions);
    return `--lineage-tint-${hashIndex(root) + 1}`;
}
