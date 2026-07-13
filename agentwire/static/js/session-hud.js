/**
 * Session HUD — top-edge frosted drawer (foundation shell, #776).
 *
 * Slides down from the top edge of the desktop area (right of the sidebar).
 * Two detents — peek (~33vh) and half (~50vh) — set by dragging the
 * top-center pull handle, which snaps to the nearest of closed/peek/half on
 * release. Clicking the handle (no drag) toggles open/closed.
 *
 * `.session-hud-canvas` is the mount point session-hud-controller.js (#778)
 * fills with TopologyView. Mirrors scratchpad.js's create-once drawer
 * lifecycle (`.open` class, keyboard toggle, teardown).
 *
 * Toggle: Alt+T or the handle. Mutually exclusive with the left sidebar and
 * the right scratchpad drawer — mirrors their existing coordination
 * (sidebar.js:72): opening the HUD closes both, and opening either of them
 * closes the HUD.
 *
 * Header (#779): a Sessions|Services segmented control sits above the
 * canvas. Sessions is the topology mounted into `.session-hud-canvas`;
 * Services reuses sidebar/services-section.js's singleton, mounted into the
 * sibling `.session-hud-services` container — same fetch/render/start-stop
 * logic as the sidebar's Services accordion, no duplication. Switching
 * segments only toggles CSS visibility (`data-segment` on the drawer); the
 * topology is never unmounted, so its live state and focus-rerooting
 * survive a round trip. Last-selected segment persists in localStorage.
 */

import { sidebar } from './sidebar.js';
import { scratchpad } from './scratchpad.js';
import { servicesSection } from './sidebar/services-section.js';

const PEEK_VH = 0.33;
const HALF_VH = 0.50;
const CLOSE_THRESHOLD = PEEK_VH / 2;
const MID_THRESHOLD = (PEEK_VH + HALF_VH) / 2;
const DRAG_MIN_VH = 0.12;
const DRAG_MAX_VH = 0.66;
const CLICK_TOLERANCE_PX = 3;

/** localStorage key for the last-selected header segment (#779). */
const SEGMENT_KEY = 'aw-hud-segment';

function clamp(v, min, max) {
    return Math.min(Math.max(v, min), max);
}

class SessionHud {
    constructor() {
        this.drawer = null;
        this.handle = null;
        this.canvas = null;
        this.open = false;
        this.detent = 'peek';
        /** @type {string|null} detent to restore on restoreDetent(), set by growToHalf() */
        this._grownFromDetent = null;
        /** @type {HTMLElement|null} header strip hosting the Sessions|Services segmented control (#779) */
        this.header = null;
        /** @type {HTMLElement|null} sibling mount point for the Services segment's content */
        this.servicesCanvas = null;
        /** @type {'sessions'|'services'} currently active header segment */
        this.segment = 'sessions';
        this._servicesMounted = false;
    }

    init() {
        this._buildDrawer();

        // Alt+T toggles the drawer. Capture phase + stopPropagation so xterm
        // never sees the keystroke — mirrors scratchpad.js's Alt+N binding.
        // e.code (not e.key): physical-key detection, consistent with every
        // other Alt combo in the portal. Option+T isn't a macOS dead key (it
        // types a literal † rather than composing), so no suppressor arm is
        // needed here — same reasoning as the Alt+bracket window-cycle combo.
        window.addEventListener('keydown', (e) => {
            if (e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyT') {
                e.preventDefault();
                e.stopPropagation();
                if (e.repeat) return;
                this.toggle();
            }
        }, true);
    }

    // ─── DOM ────────────────────────────────────────────────────

    _buildDrawer() {
        const drawer = document.createElement('div');
        drawer.className = 'session-hud-drawer';
        drawer.innerHTML = `
            <div class="session-hud-header">
                <div class="session-hud-segmented" role="tablist" aria-label="Session HUD view">
                    <button type="button" class="session-hud-segment-btn" data-segment="sessions" role="tab">Sessions</button>
                    <button type="button" class="session-hud-segment-btn" data-segment="services" role="tab">Services</button>
                </div>
            </div>
            <div class="session-hud-canvas"></div>
            <div class="session-hud-services"></div>
        `;
        document.body.appendChild(drawer);
        this.drawer = drawer;
        this.header = drawer.querySelector('.session-hud-header');
        this.canvas = drawer.querySelector('.session-hud-canvas');
        this.servicesCanvas = drawer.querySelector('.session-hud-services');

        this.header.querySelectorAll('.session-hud-segment-btn').forEach((btn) => {
            btn.addEventListener('click', () => this.setSegment(btn.dataset.segment));
        });
        this._applySegment(this._loadSegment());

        const handle = document.createElement('button');
        handle.className = 'session-hud-handle';
        handle.title = 'Session HUD (Alt+T)';
        handle.innerHTML = '<span class="session-hud-grip" aria-hidden="true"></span>';
        document.body.appendChild(handle);
        this.handle = handle;

        this._wireHandleDrag();
    }

    _wireHandleDrag() {
        const handle = this.handle;
        let dragging = false;
        let moved = false;
        let startY = 0;
        let startHeight = 0;

        const onMove = (e) => {
            if (!dragging) return;
            const dy = e.clientY - startY;
            if (!moved && Math.abs(dy) <= CLICK_TOLERANCE_PX) return;
            moved = true;
            const heightPx = clamp(
                startHeight + dy,
                window.innerHeight * DRAG_MIN_VH,
                window.innerHeight * DRAG_MAX_VH,
            );
            this._applyDragHeight(heightPx);
        };

        const onUp = (e) => {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('dragging');
            this.drawer.classList.remove('dragging');
            handle.releasePointerCapture?.(e.pointerId);
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);

            if (!moved) {
                this.toggle();
                return;
            }
            const fraction = this.drawer.getBoundingClientRect().height / window.innerHeight;
            if (fraction < CLOSE_THRESHOLD) {
                this.toggle(false);
            } else if (fraction < MID_THRESHOLD) {
                this._settle('peek');
            } else {
                this._settle('half');
            }
        };

        handle.addEventListener('pointerdown', (e) => {
            if (e.button !== undefined && e.button !== 0) return;
            e.preventDefault();
            dragging = true;
            moved = false;
            startY = e.clientY;
            startHeight = this.open ? this.drawer.getBoundingClientRect().height : 0;
            handle.classList.add('dragging');
            this.drawer.classList.add('dragging');
            handle.setPointerCapture?.(e.pointerId);
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
        });
    }

    _applyDragHeight(heightPx) {
        if (!this.open) {
            this.open = true;
            this.drawer.classList.add('open');
            this.handle.classList.add('drawer-open');
        }
        this.drawer.style.height = `${heightPx}px`;
        this.handle.style.top = `${heightPx}px`;
    }

    _settle(detent) {
        this.detent = detent;
        this.toggle(true);
    }

    /**
     * Programmatically grow to the half detent (e.g. a HUD card's mini-terminal
     * just opened and needs the room) — remembers whatever detent was active
     * so restoreDetent() can put it back. A no-op if already grown (the
     * accordion-style switch between two expanded cards collapses the old
     * one — which calls restoreDetent() — then expands the new one — which
     * calls this again — within the same tick; only the first grab of a grow
     * cycle should record what to restore).
     */
    growToHalf() {
        if (this._grownFromDetent !== null) return;
        this._grownFromDetent = this.detent;
        if (this.detent !== 'half') this._settle('half');
    }

    /** Undo growToHalf() — restores the detent that was active before the
     * grow. No-op if nothing is currently grown. */
    restoreDetent() {
        if (this._grownFromDetent === null) return;
        const prior = this._grownFromDetent;
        this._grownFromDetent = null;
        if (this.detent !== prior) this._settle(prior);
    }

    /**
     * Auto-peek for a spawn (#780) — opens to the peek detent (~33vh) if
     * currently closed. A no-op if already open: an open HUD means the user
     * is already looking at it (or grew it to half for a mini-terminal), and
     * a spawn shouldn't yank it to a different detent out from under them.
     * Returns whether it actually opened, so a caller knows whether it now
     * owns retracting the HUD again later.
     */
    peekForSpawn() {
        if (this.open) return false;
        this.detent = 'peek';
        this.toggle(true);
        return true;
    }

    // ─── Header segments (#779) ────────────────────────────────

    _loadSegment() {
        try {
            return localStorage.getItem(SEGMENT_KEY) === 'services' ? 'services' : 'sessions';
        } catch (e) {
            return 'sessions';
        }
    }

    /**
     * Switch the HUD header between the Sessions topology and the Services
     * list. Swaps visibility only (`data-segment` on the drawer drives the
     * CSS) — the topology canvas is never unmounted, so session-hud-controller's
     * live render/focus-rerooting keeps running underneath and is exactly as
     * it was when the user switches back.
     */
    setSegment(segment) {
        if (segment !== 'sessions' && segment !== 'services') return;
        try { localStorage.setItem(SEGMENT_KEY, segment); } catch (e) {}
        this._applySegment(segment);
    }

    _applySegment(segment) {
        this.segment = segment;
        this.drawer.dataset.segment = segment;
        this.header.querySelectorAll('.session-hud-segment-btn').forEach((btn) => {
            const active = btn.dataset.segment === segment;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', String(active));
        });
        if (segment !== 'services') return;
        // Reuse the sidebar's servicesSection singleton (SSOT for the
        // fetch/render/start-stop logic) — mount once into our own
        // container, then just re-render on every subsequent visit since
        // its onSessionsChanged subscription already keeps content live
        // while hidden.
        if (!this._servicesMounted) {
            this._servicesMounted = true;
            servicesSection.mount(this.servicesCanvas);
        } else {
            servicesSection.refresh(this.servicesCanvas);
        }
    }

    // ─── State ──────────────────────────────────────────────────

    toggle(force = null) {
        const next = force ?? !this.open;
        this.drawer.style.height = '';
        this.handle.style.top = '';
        if (next) {
            this.drawer.classList.toggle('detent-half', this.detent === 'half');
            this.handle.classList.toggle('detent-half', this.detent === 'half');
            this.open = true;
            this.drawer.classList.add('open');
            this.handle.classList.add('drawer-open');
            sidebar.close();
            if (scratchpad.open) scratchpad.toggle(false);
        } else {
            this.open = false;
            this.drawer.classList.remove('open');
            this.handle.classList.remove('drawer-open');
        }
    }
}

export const sessionHud = new SessionHud();
