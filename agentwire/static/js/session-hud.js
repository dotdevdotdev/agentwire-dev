/**
 * Session HUD — top-edge frosted drawer (foundation shell, #776).
 *
 * Slides down from the top edge of the desktop area (right of the sidebar).
 * Two detents — peek and half (~50vh) — set by dragging the top-center pull
 * handle, which snaps to the nearest of closed/peek/half on release.
 * Clicking the handle (no drag) toggles open/closed.
 *
 * "Peek" auto-sizes to content (#802) rather than a fixed 33vh: it tracks
 * `.session-hud-canvas`/`.session-hud-services`' `scrollHeight` (whichever
 * segment is active) via a MutationObserver, floored at DRAG_MIN_VH so a
 * single card never shrinks the drawer to nothing and capped at HALF_VH so
 * it never grows past the "half" detent on its own — half stays a fixed,
 * content-independent ceiling, reachable by dragging past the midpoint or
 * via growToHalf(). PEEK_VH itself no longer renders anything; it only
 * anchors the drag-release snap thresholds below.
 *
 * `.session-hud-canvas` is the mount point session-hud-controller.js (#778)
 * fills with TopologyView. Mirrors scratchpad.js's create-once drawer
 * lifecycle (`.open` class, keyboard toggle, teardown).
 *
 * Toggle: Alt+P ("peek") or the handle. Mutually exclusive with the left sidebar and
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
 *
 * Notices strip (#817): `.session-hud-notices` sits between the header and
 * the segment canvases, visible in BOTH segments — session-hud-notices.js
 * renders pending artifact notifications (click-to-open deliverables not
 * tied to any live session card) into it. Its height joins the header's in
 * the peek auto-size sum below.
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
/** Reserved gap between the last card and the drawer's border-bottom in the
 * peek auto-size sum (#823). Deliberate and segment-independent: the mounted
 * content's own bottom padding (8px on .topology-view--shade, 12px on
 * .session-hud-services) is incidental and not guaranteed, and measurement
 * timing slop (rAF firing before an in-progress card animation settles) can
 * leave the real render slightly taller than what was measured — this margin
 * absorbs both so content never sits flush against the border. */
const PEEK_BOTTOM_MARGIN_PX = 10;

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
        /** @type {HTMLElement|null} pending artifact-notice strip, filled by session-hud-notices.js (#817) */
        this.noticesEl = null;
        /** @type {HTMLElement|null} sibling mount point for the Services segment's content */
        this.servicesCanvas = null;
        /** @type {'sessions'|'services'} currently active header segment */
        this.segment = 'sessions';
        this._servicesMounted = false;
        /** @type {Array<() => void>} fired when the drawer closes — lets the
         * controller drop its pinned "master/global" view (see showAll). */
        this._closeListeners = [];
        /** @type {boolean} true while the handle is mid-drag — auto-height
         * recomputes stand down so they don't fight the user's own gesture. */
        this._dragging = false;
        /** @type {number|null} pending rAF handle for a debounced auto-height pass */
        this._autoRaf = null;
    }

    /** Subscribe to drawer-close. */
    onClose(fn) {
        this._closeListeners.push(fn);
    }

    init() {
        this._buildDrawer();

        // Alt+P ("peek") toggles the drawer. Moved off Alt+T once Claude Code
        // claimed Option+T for toggle-thinking — our capture-phase handler would
        // otherwise swallow it before it reached a session running Claude Code.
        // Capture phase + stopPropagation so xterm never sees the keystroke —
        // mirrors scratchpad.js's Alt+N binding. e.code (not e.key):
        // physical-key detection, consistent with every other Alt combo in the
        // portal. Option+P isn't a macOS dead key (it types a literal π rather
        // than composing), so no suppressor arm is needed — same reasoning as
        // the Alt+bracket window-cycle combo.
        window.addEventListener('keydown', (e) => {
            if (e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyP') {
                e.preventDefault();
                e.stopPropagation();
                if (e.repeat) return;
                this.toggle();
            }
        }, true);

        // The vh-based floor/cap move with the viewport (e.g. a resized
        // browser window) — re-run auto-height so a stale px value doesn't
        // linger outside the current clamp range.
        window.addEventListener('resize', () => this._scheduleAutoHeight());
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
            <div class="session-hud-notices" hidden></div>
            <div class="session-hud-canvas"></div>
            <div class="session-hud-services"></div>
        `;
        document.body.appendChild(drawer);
        this.drawer = drawer;
        this.header = drawer.querySelector('.session-hud-header');
        this.noticesEl = drawer.querySelector('.session-hud-notices');
        this.canvas = drawer.querySelector('.session-hud-canvas');
        this.servicesCanvas = drawer.querySelector('.session-hud-services');

        this._contentObserver = new MutationObserver(() => this._scheduleAutoHeight());

        this.header.querySelectorAll('.session-hud-segment-btn').forEach((btn) => {
            btn.addEventListener('click', () => this.setSegment(btn.dataset.segment));
        });
        this._applySegment(this._loadSegment());

        const handle = document.createElement('button');
        handle.className = 'session-hud-handle';
        handle.title = 'Session HUD (Alt+P)';
        handle.innerHTML = '<span class="session-hud-grip" aria-hidden="true"></span>';
        document.body.appendChild(handle);
        this.handle = handle;

        this._wireHandleDrag();
    }

    /** (Re)targets the content MutationObserver at whichever segment is
     * currently visible — `_contentHeightPx()` only ever reads that one, so
     * watching the hidden segment too would just schedule wasted recomputes
     * of an unchanged, already-correct height (the segment switch itself
     * re-measures fresh via `_applySegment()`). Rows/cards are added and
     * removed as childList mutations (family/row/card elements — see
     * topology-render.js), which covers "grow as rows are added"; `hidden`/
     * `class` are also watched because a ghost↔live card swap (#781) or a
     * card's live-state dot only flip those attributes — no element is
     * added or removed, so childList alone would miss a card that got
     * visibly taller or shorter without changing the DOM's shape. */
    _observeContent() {
        this._contentObserver.disconnect();
        const el = this.segment === 'services' ? this.servicesCanvas : this.canvas;
        this._contentObserver.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'class'] });
        // The notices strip (#817) is segment-independent and part of the
        // auto-size sum, so it's watched alongside whichever segment is up —
        // a notice arriving/clearing must resize the open drawer too.
        this._contentObserver.observe(this.noticesEl, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'class'] });
    }

    _wireHandleDrag() {
        const handle = this.handle;
        let moved = false;
        let startY = 0;
        let startHeight = 0;

        const onMove = (e) => {
            if (!this._dragging) return;
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

        // Shared teardown for both a normal release and an interrupted
        // gesture (OS/browser-cancelled pointer capture, e.g. a touch
        // scroll takeover or focus loss mid-drag). `_dragging` is no longer
        // just a cosmetic flag — `_applyAutoHeight()` bails out while it's
        // true — so a `pointerup` that never arrives must not leave it
        // stuck forever, or auto-sizing silently stops for the rest of the
        // page's life.
        const endDrag = (e) => {
            this._dragging = false;
            handle.classList.remove('dragging');
            this.drawer.classList.remove('dragging');
            handle.releasePointerCapture?.(e.pointerId);
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            window.removeEventListener('pointercancel', onCancel);
        };

        const onUp = (e) => {
            if (!this._dragging) return;
            endDrag(e);

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

        // No reliable final position on a cancel — just drop the gesture in
        // place (leave the drawer at its current height) rather than
        // guessing a detent to snap to.
        const onCancel = (e) => {
            if (!this._dragging) return;
            endDrag(e);
        };

        handle.addEventListener('pointerdown', (e) => {
            if (e.button !== undefined && e.button !== 0) return;
            e.preventDefault();
            this._dragging = true;
            moved = false;
            startY = e.clientY;
            startHeight = this.open ? this.drawer.getBoundingClientRect().height : 0;
            handle.classList.add('dragging');
            this.drawer.classList.add('dragging');
            handle.setPointerCapture?.(e.pointerId);
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
            window.addEventListener('pointercancel', onCancel);
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

    /** Natural content height of whichever header segment is currently
     * visible. Measure the canvas's *content children*, not the canvas itself:
     * the canvas is `flex:1 1 0` and stretches to fill the drawer, so its own
     * `scrollHeight` is floored by its `clientHeight` and can never report
     * *less* than the current drawer height — which let auto-size grow (content
     * overflows) but never shrink below the peek detent for a single-card view
     * (#802). The children are content-sized in both directions, so their
     * rendered heights + the canvas's own vertical padding is the height the
     * canvas would take at rest. Summed over ALL children (+ their vertical
     * margins), not just the first: the topology mounts a single
     * `.topology-view` root, but the services section renders a flat list of
     * sibling cards — measuring only the first child sized the drawer to one
     * card and left the rest scrolled out of view (#823). */
    _contentHeightPx() {
        const el = this.segment === 'services' ? this.servicesCanvas : this.canvas;
        if (!el) return 0;
        // A classic (non-overlay) horizontal scrollbar consumes canvas inner
        // height without appearing in the children's own measured heights —
        // omit it and the drawer clips exactly that many px off the last
        // row of cards (#823). offsetHeight−clientHeight is its current
        // rendered thickness (0 for overlay scrollbars or no horizontal
        // overflow), and it's stable across the height change this measure
        // feeds: presence depends on content width vs canvas width, not on
        // the drawer's height.
        const scrollbarPx = el.offsetHeight - el.clientHeight;
        if (!el.firstElementChild) return el.scrollHeight + scrollbarPx;
        const cs = getComputedStyle(el);
        let heightPx = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + scrollbarPx;
        for (const child of el.children) {
            const ccs = getComputedStyle(child);
            heightPx += child.getBoundingClientRect().height
                + parseFloat(ccs.marginTop) + parseFloat(ccs.marginBottom);
        }
        return heightPx;
    }

    /** Debounced entry point for content-driven mutations (MutationObserver,
     * window resize) — coalesces a burst of DOM changes from one render pass
     * into a single measure-and-apply. */
    _scheduleAutoHeight() {
        if (this._autoRaf !== null) return;
        this._autoRaf = requestAnimationFrame(() => {
            this._autoRaf = null;
            this._applyAutoHeight();
        });
    }

    /** Resize the open drawer to fit its current content, floored at
     * DRAG_MIN_VH and capped at HALF_VH — the "auto-size as the floor, half
     * stays the max" behavior (#802). No-op while closed, while grown to
     * half for a mini-terminal (growToHalf owns the height then), while
     * settled on the fixed half detent, or mid-drag (the user's gesture
     * wins). */
    _applyAutoHeight() {
        if (!this.open || this.detent !== 'peek' || this._grownFromDetent !== null || this._dragging) return;
        const headerPx = this.header ? this.header.getBoundingClientRect().height : 0;
        const noticesPx = this.noticesEl && !this.noticesEl.hidden
            ? this.noticesEl.getBoundingClientRect().height : 0;
        const rawPx = headerPx + noticesPx + this._contentHeightPx() + PEEK_BOTTOM_MARGIN_PX;
        const heightPx = clamp(rawPx, window.innerHeight * DRAG_MIN_VH, window.innerHeight * HALF_VH);
        this.drawer.style.height = `${heightPx}px`;
        this.handle.style.top = `${heightPx}px`;
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
     * Auto-peek for a spawn (#780) — opens to the peek detent (auto-sized to
     * content, #802) if currently closed. A no-op if already open: an open
     * HUD means the user is already looking at it (or grew it to half for a
     * mini-terminal), and
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
        // Reuse the sidebar's servicesSection singleton (SSOT for the
        // fetch/render/start-stop logic) — mount once into our own
        // container, then just re-render on every subsequent visit since
        // its onSessionsChanged subscription already keeps content live
        // while hidden.
        if (segment === 'services') {
            if (!this._servicesMounted) {
                this._servicesMounted = true;
                servicesSection.mount(this.servicesCanvas);
            } else {
                servicesSection.refresh(this.servicesCanvas);
            }
        }
        this._observeContent();
        this._scheduleAutoHeight();
    }

    // ─── State ──────────────────────────────────────────────────

    toggle(force = null) {
        const next = force ?? !this.open;
        if (next) {
            this.drawer.classList.toggle('detent-half', this.detent === 'half');
            this.handle.classList.toggle('detent-half', this.detent === 'half');
            this.open = true;
            this.drawer.classList.add('open');
            this.handle.classList.add('drawer-open');
            sidebar.close();
            if (scratchpad.open) scratchpad.toggle(false);
            // 'half' is a fixed CSS-driven height (see .detent-half) — only
            // 'peek' (auto-size) needs a JS-measured inline height applied.
            // Scheduled rather than applied inline: some callers (e.g.
            // session-hud-controller.js's showAll()) open the drawer BEFORE
            // re-rendering the canvas for the new context, so measuring
            // synchronously here would size the drawer to the stale,
            // about-to-be-replaced content — deferring a frame lets that
            // synchronous re-render land first.
            if (this.detent === 'peek') {
                this._scheduleAutoHeight();
            } else {
                this.drawer.style.height = '';
                this.handle.style.top = '';
            }
        } else {
            this.drawer.style.height = '';
            this.handle.style.top = '';
            this.open = false;
            this.drawer.classList.remove('open');
            this.handle.classList.remove('drawer-open');
            // A card left expanded (growToHalf'd) when the drawer itself
            // closes — e.g. Alt+P, or the sidebar/scratchpad's own
            // mutually-exclusive close — never gets its matching
            // restoreDetent() (that only fires when the CARD collapses).
            // Left stale, _applyAutoHeight()'s grown-guard would silently
            // no-op forever on every future peek-open. Closing the drawer
            // is a clean boundary to drop that leftover bookkeeping.
            this._grownFromDetent = null;
            this._closeListeners.forEach((fn) => fn());
        }
    }
}

export const sessionHud = new SessionHud();
