/**
 * session-hud-spawn.js
 *
 * Spawn choreography for the Session HUD (#780) — absorbs what the deleted
 * phantom overlay (topology-overlay.js, #764) used to do standalone: the
 * instant a child session spawns, desktop.js's `handleSessionCreated` calls
 * `triggerSpawnPeek()` here, which:
 *   1. auto-peeks the HUD to ~33vh if it's closed (gated on the "auto-peek
 *      on spawn" pref below, on by default — a no-op, detent untouched, if
 *      the HUD is already open),
 *   2. flies a spawn-ghost.js ghost (#745) from the parent's card to the new
 *      child's card — both are already real DOM nodes by this point, since
 *      session-hud-controller.js's own `onSessionsChanged` render placed
 *      them before this runs,
 *   3. retracts the HUD after a linger — but only when THIS module opened it
 *      in step 1, and only if the user hasn't grabbed the pull handle
 *      meanwhile (grabbing cancels the retract outright, not just delays
 *      it — don't yank the surface out from under an active drag).
 *
 * Never builds or mutates a topology card itself (that stays entirely
 * TopologyView's job) — this only draws a transient ghost on top and drives
 * sessionHud's own open/close, both through its public API.
 *
 * @module session-hud-spawn
 */

import { sessionHud } from './session-hud.js';
import { flyGhost } from './spawn-ghost.js';
import { lineageTintVar } from './lineage.js';

const AUTOPEEK_KEY = 'aw-hud-autopeek-on-spawn';
export const HUD_AUTOPEEK_EVENT = 'hud-autopeek-change';

/** Mirrors the deleted topology-overlay.js's LINGER_MS (#764) — long enough
 * to register as "a session was just born" before the HUD retracts on its
 * own. */
const LINGER_MS = 2600;

/** Above the HUD drawer/handle (z 9001/9002) so the fly-in ghost paints over
 * the frosted glass instead of being hidden behind it — same "above
 * WinBoxes and both drawers" tier the scratchpad selection-capture popover
 * uses (desktop.css, z 9100). */
const HUD_GHOST_Z = 9100;

/** --sidebar-transition (180ms, desktop.css) + buffer — how long to wait for
 * the drawer's slide-open transform to settle before measuring card rects,
 * so the ghost flies toward a rect that's actually at rest. */
const DRAWER_SETTLE_MS = 230;

let lingerTimer = null;
let cancelGrabWatch = null;

export function isAutoPeekEnabled() {
    return localStorage.getItem(AUTOPEEK_KEY) !== '0';
}

export function setAutoPeekEnabled(v) {
    if (v) localStorage.removeItem(AUTOPEEK_KEY);
    else localStorage.setItem(AUTOPEEK_KEY, '0');
    window.dispatchEvent(new CustomEvent(HUD_AUTOPEEK_EVENT));
}

/**
 * Call the instant a session_created event names a child with a known
 * parent. No-op when the HUD is closed and the auto-peek pref is off — the
 * HUD stays the way the user left it.
 *
 * @param {string} sessionName
 * @param {Array<object>} allSessions
 */
export function triggerSpawnPeek(sessionName, allSessions) {
    if (!sessionName) return;
    _cancelLinger();

    const wasOpen = sessionHud.open;
    if (!wasOpen) {
        if (!isAutoPeekEnabled()) return;
        sessionHud.peekForSpawn();
    }

    if (wasOpen) {
        _flyCard(sessionName, allSessions);
        return;
    }

    // Wait for the drawer's slide-open transform to finish before measuring
    // card rects — reading them mid-transition would fly the ghost toward a
    // still-moving target. transitionend has a safety-net timeout for the
    // same reason spawn-ghost.js's own fly has one: a backgrounded tab or a
    // property that didn't actually change can fail to fire it.
    let done = false;
    const finish = () => {
        if (done) return;
        done = true;
        sessionHud.drawer.removeEventListener('transitionend', onEnd);
        _flyCard(sessionName, allSessions);
        _armLinger();
    };
    const onEnd = (e) => { if (e.propertyName === 'transform') finish(); };
    sessionHud.drawer.addEventListener('transitionend', onEnd);
    setTimeout(finish, DRAWER_SETTLE_MS);
}

function _flyCard(sessionName, allSessions) {
    const canvas = sessionHud.canvas;
    if (!canvas) return;
    const childEl = canvas.querySelector(`.topology-card[data-session="${CSS.escape(sessionName)}"]`);
    if (!childEl) return; // not in the current HUD view (e.g. re-rooted onto a different family)

    const session = (allSessions || []).find((s) => s.name === sessionName);
    const parentName = session?.parent || null;
    const parentEl = parentName
        ? canvas.querySelector(`.topology-card[data-session="${CSS.escape(parentName)}"]`)
        : null;

    const toRect = childEl.getBoundingClientRect();
    const fromRect = parentEl ? parentEl.getBoundingClientRect() : null;
    const tintVar = lineageTintVar(sessionName, allSessions);
    // flyGhost() itself handles the graceful fallback (no fromRect, or
    // prefers-reduced-motion) — the real card is already rendered either
    // way, so onSettle has nothing left to do.
    flyGhost(fromRect, toRect, tintVar, () => {}, HUD_GHOST_Z);
}

function _armLinger() {
    const onGrab = () => _cancelLinger();
    sessionHud.handle.addEventListener('pointerdown', onGrab, { once: true });
    cancelGrabWatch = () => sessionHud.handle.removeEventListener('pointerdown', onGrab);
    lingerTimer = setTimeout(() => {
        lingerTimer = null;
        cancelGrabWatch?.();
        cancelGrabWatch = null;
        sessionHud.toggle(false);
    }, LINGER_MS);
}

function _cancelLinger() {
    if (lingerTimer) {
        clearTimeout(lingerTimer);
        lingerTimer = null;
    }
    cancelGrabWatch?.();
    cancelGrabWatch = null;
}
