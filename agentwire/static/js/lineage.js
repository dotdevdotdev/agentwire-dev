/**
 * lineage.js
 *
 * Maps a session to its family's lineage-tint CSS variable (#749 SSOT —
 * `agentwire/static/css/desktop.css` `--lineage-tint-1..6`). A family is a
 * root session plus every descendant reachable by walking `.parent` links;
 * the whole family shares one hue so relatedness reads at a glance without
 * labels. Consumed by the born-from-parent ghost (#745) and meant to be
 * reused by the connector overlay / grouped collage slices rather than each
 * re-deriving its own palette.
 *
 * @module lineage
 */

const TINT_COUNT = 6;

/**
 * Walk `.parent` links from `name` up to its root ancestor.
 * Tolerant of missing sessions, self-referencing parents, and cycles (caps
 * the walk depth rather than looping forever).
 *
 * @param {string} name - Session name to resolve.
 * @param {Array<{name: string, parent?: string|null}>} sessions - Full session list.
 * @returns {string} The root ancestor's name (or `name` itself if it has no parent).
 */
export function familyRootName(name, sessions) {
    const byName = new Map((sessions || []).map((s) => [s.name, s]));
    let current = byName.get(name);
    let depth = 0;
    while (
        current &&
        current.parent &&
        current.parent !== current.name &&
        byName.has(current.parent) &&
        depth < 32
    ) {
        current = byName.get(current.parent);
        depth++;
    }
    return current ? current.name : name;
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
