/**
 * spawn-ghost.js
 *
 * "Born from parent" placement (#745): flies a plain overlay div from a
 * parent window's title-bar edge to the landing spot, then hands off to the
 * real (already-settled) session window. This module never touches a real
 * WinBox window's geometry — same shadow-layer invariant as collage.js, and
 * for the same reason (#235): transforming a real window mid-open corrupts
 * WinBox's internal stack and fires ResizeObserver into tmux PTY resize
 * storms. The ghost is the only thing that moves; the real window is only
 * ever created once the ghost has already settled at rest.
 *
 * @module spawn-ghost
 */

/** Above windows (WinBox's focus counter grows z-index from 10), below
 * toasts (1500) — same band as collage.js's OVERLAY_Z (1400). */
const GHOST_Z = 1420;

/** Exported so other spawn-triggered animations (session-hud-spawn.js, #780)
 * share the exact same fly duration instead of picking their own — one
 * "how long does a spawn animation take" constant, not two that can drift
 * apart. */
export const FLY_MS = 480;

let root = null;

function ensureRoot() {
    if (root) return root;
    root = document.createElement('div');
    root.className = 'spawn-ghost-root';
    root.style.zIndex = String(GHOST_Z);
    document.body.appendChild(root);
    return root;
}

/** Exported so other spawn-triggered animations (session-hud-spawn.js, #780)
 * make the same reduced-motion call this module does, rather than each
 * re-querying matchMedia. */
export function prefersReducedMotion() {
    return typeof window !== 'undefined' &&
        typeof window.matchMedia === 'function' &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Fly a ghost from `fromRect` to `toRect`, then invoke `onSettle`.
 *
 * Skips the animation and calls `onSettle` synchronously when there's no
 * usable start rect (parent window not open/minimized — the graceful
 * fallback) or the user prefers reduced motion — in both cases placement
 * still happens, just instantly.
 *
 * @param {DOMRect|null} fromRect - Parent's title-bar rect, viewport coords.
 * @param {DOMRect} toRect - Landing spot (the desktop area), viewport coords.
 * @param {string} tintVar - A `--lineage-tint-N` custom property name.
 * @param {Function} onSettle - Called once the ghost is at rest (or immediately if skipped).
 * @param {number} [zIndex=GHOST_Z] - Stacking tier for this fly. Defaults to the
 *   window-birth tier; callers flying over something stacked above that (e.g. the
 *   Session HUD drawer, z 9001) pass a higher value so the ghost isn't painted
 *   underneath it.
 */
export function flyGhost(fromRect, toRect, tintVar, onSettle, zIndex = GHOST_Z) {
    if (!fromRect || !toRect || prefersReducedMotion()) {
        onSettle();
        return;
    }

    const el = document.createElement('div');
    el.className = 'spawn-ghost';
    el.style.setProperty('--ghost-tint', `var(${tintVar})`);
    el.style.left = `${fromRect.left}px`;
    el.style.top = `${fromRect.top}px`;
    el.style.width = `${fromRect.width}px`;
    el.style.height = `${fromRect.height}px`;

    const ghostRoot = ensureRoot();
    ghostRoot.style.zIndex = String(zIndex);
    ghostRoot.appendChild(el);

    // Force the browser to commit the start rect before the end-rect write
    // below, or both writes coalesce into one frame and nothing transitions.
    void el.getBoundingClientRect();

    requestAnimationFrame(() => {
        el.classList.add('spawn-ghost--fly');
        el.style.left = `${toRect.left}px`;
        el.style.top = `${toRect.top}px`;
        el.style.width = `${toRect.width}px`;
        el.style.height = `${toRect.height}px`;
    });

    let done = false;
    const finish = () => {
        if (done) return;
        done = true;
        el.remove();
        onSettle();
    };
    el.addEventListener('transitionend', finish, { once: true });
    // Safety net — transitionend can fail to fire (tab backgrounded mid-fly,
    // a property that didn't actually change); never leave the ghost or the
    // caller hanging.
    setTimeout(finish, FLY_MS + 250);
}
