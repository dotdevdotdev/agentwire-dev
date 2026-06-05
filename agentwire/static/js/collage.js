/**
 * Collage - window collage overlay.
 *
 * Lays every open window into a grid so the whole desktop can be scanned at once.
 * Click any tile to focus that window and exit; Esc exits restoring the prior state.
 *
 * The portal runs in single-window mode (one maximized window, the rest minimized),
 * so "prior state" is just: which window was active + each window's minimized flag +
 * any tile zones.
 *
 * Tiles are the live windows, placed via CSS `transform: translate()+scale()` — NOT
 * by resizing them. Resizing a live window that hosts an xterm down to a grid cell
 * and back corrupts its GPU compositing layer: the background renders transparent
 * and nothing (refresh, reflow, even a real click or window resize) repaints it.
 * A transform is a cheap GPU op that reuses the window's existing full-size raster,
 * so the layer never re-rasters small. The active window's geometry is left fully
 * untouched (only transformed); minimized windows are first grown to the full box,
 * then scaled into their cell. Exit just drops the transform.
 *
 * @module collage
 */

import { desktop } from './desktop-manager.js';
import { tileManager } from './tile-manager.js';

/** z-index band: scrim sits at BASE, grid windows above it. */
const SCRIM_Z = 5000;

class Collage {
    constructor() {
        /** @type {boolean} */
        this._active = false;

        /** @type {HTMLElement|null} */
        this._scrim = null;

        /** @type {Object|null} Pre-collage state snapshot */
        this._snapshot = null;

        /** @type {Array<{el: HTMLElement, fn: Function}>} Per-window click handlers */
        this._clickHandlers = [];

        /** @type {Array<Function>} desktop event unsubscribe fns (active only) */
        this._unsubs = [];

        /** @type {function(string): (object|null)} id → window instance lookup */
        this._lookup = () => null;

        this._onKeydown = this._onKeydown.bind(this);
    }

    /**
     * Initialize. Creates the scrim element (hidden) inside the desktop area.
     * @param {function(string): (object|null)} [lookupInstance] - Resolves a
     *   window id to its SessionWindow/ArtifactWindow instance (for refit on exit).
     */
    init(lookupInstance) {
        if (typeof lookupInstance === 'function') this._lookup = lookupInstance;
        const area = document.getElementById('desktopArea');
        if (!area) return;
        this._scrim = document.createElement('div');
        this._scrim.className = 'collage-overlay hidden';
        this._scrim.style.zIndex = String(SCRIM_Z);
        this._scrim.addEventListener('click', () => this.exit());
        area.appendChild(this._scrim);
    }

    /**
     * Toggle the overlay.
     */
    toggle() {
        this._active ? this.exit() : this.enter();
    }

    /**
     * Enter Collage: restore every window and lay them into a grid.
     */
    enter() {
        if (this._active) return;
        const ids = [...desktop.windows.keys()];
        if (ids.length < 2) return;  // nothing to collage

        // Snapshot the single-window state so we can restore it on Esc.
        const minimized = new Map();
        for (const [id, winbox] of desktop.windows) {
            minimized.set(id, !!winbox.min);
        }
        this._snapshot = {
            activeId: desktop.getActiveWindow(),
            minimized,
            tileStates: new Map(desktop.tileStates),
        };

        // Clear tile states BEFORE laying out — otherwise tile-manager's
        // window_restored handler re-tiles each window to its old zone, fighting
        // the grid. The snapshot above lets us put them back on exit.
        desktop.tileStates.clear();

        // macOS Option+` is a dead key: its grave accent commits via the
        // composition/input path (not keydown), so stopPropagation on the hotkey
        // can't block it leaking into the focused terminal. Blur the active
        // element so the composed ` has no target; _refitActive refocuses on exit.
        if (document.activeElement && typeof document.activeElement.blur === 'function') {
            document.activeElement.blur();
        }

        this._active = true;
        this._showScrim();
        this._layout(ids);

        document.addEventListener('keydown', this._onKeydown, true);
        this._unsubs.push(
            desktop.on('window_registered', () => this._relayout()),
            desktop.on('window_unregistered', () => this._relayout()),
            desktop.on('session_closed', () => this._relayout()),
            // External activation (Tab cycle, sidebar click) sets single-window
            // mode out from under us — tear the overlay down cleanly instead of
            // leaving a stale active state that fights the next toggle.
            desktop.on('active_window_changed', () => { this._snapshot = null; this._teardown(); }),
        );
    }

    /**
     * Exit Collage.
     * @param {string|null} focusId - If given, that window becomes the single
     *   maximized window (click-to-focus). Otherwise the pre-collage state restores.
     */
    exit(focusId = null) {
        if (!this._active) return;

        const snap = this._snapshot;
        this._snapshot = null;
        this._teardown();  // sets _active=false, removes scrim/handlers/transform

        if (focusId && desktop.windows.has(focusId)) {
            // Commit to the clicked window: single-window mode, rest minimized.
            desktop.setActiveWindow(focusId);
        } else if (snap) {
            // Esc/release: restore exact prior state — re-tile what was tiled, then
            // re-maximize the prior active window (setActiveWindow minimizes others).
            for (const [id, zone] of snap.tileStates) {
                if (desktop.windows.has(id)) tileManager._tileWindow(id, zone);
            }
            if (snap.activeId && desktop.windows.has(snap.activeId) && !desktop.tileStates.has(snap.activeId)) {
                desktop.setActiveWindow(snap.activeId);
            } else if (desktop.tileStates.size === 0) {
                desktop.minimizeAllExcept(null);
            }
        }

        // Refocus the now-active window (we blurred on enter).
        this._refitActive();
    }

    /** Refocus + refit/repaint whichever window is now active (we blurred on enter). */
    _refitActive() {
        const inst = this._lookup(desktop.getActiveWindow());
        if (!inst) return;
        if (typeof inst.focus === 'function') inst.focus();
        if (typeof inst.refit === 'function') inst.refit();
    }

    /**
     * Remove all overlay chrome (scrim, key/click handlers, event subs) and the
     * collage transform, marking inactive — WITHOUT changing window placement.
     * Shared by exit() and the external-activation handler (Tab cycle / sidebar
     * click). Clearing the transform snaps each window back to its full box (its
     * raster is intact, so no corruption); setActiveWindow/minimizeAllExcept then
     * re-maximize/minimize as needed. The inline geometry box set in _layout is
     * left in place — this app's `.max` CSS only overrides top/height, so width/left
     * come from inline geometry, and WinBox's minimize overwrites it for the windows
     * that should hide.
     */
    _teardown() {
        if (!this._active) return;
        this._active = false;

        document.removeEventListener('keydown', this._onKeydown, true);
        this._unsubs.forEach((fn) => { try { fn(); } catch (e) {} });
        this._unsubs = [];
        this._clearClickHandlers();
        this._hideScrim();

        for (const [, winbox] of desktop.windows) {
            const el = winbox?.window;
            if (el) {
                el.classList.remove('tiled');
                el.style.transform = '';
                el.style.transformOrigin = '';
                el.style.zIndex = '';
            }
        }
    }

    // ============================================
    // Internals
    // ============================================

    /**
     * Lay the given windows into the grid via transforms and (re)wire
     * click-to-focus + z-index. Every tile is translated+scaled into its cell —
     * never resized (see module header for why resizing corrupts the xterm layer).
     * @param {string[]} ids
     */
    _layout(ids) {
        const area = document.getElementById('desktopArea');
        if (!area) return;
        const rect = area.getBoundingClientRect();
        const n = ids.length;

        // Grid sizing — fit cols×rows to the desktop aspect (matches the look of
        // the old tile-grid layout).
        const aspect = rect.width / rect.height;
        let cols = Math.max(1, Math.round(Math.sqrt(n * aspect)));
        cols = Math.min(cols, n);
        let rows = Math.ceil(n / cols);
        while (cols > 1 && (cols - 1) * rows >= n) cols--;
        rows = Math.ceil(n / cols);
        const gutter = 8;
        const cellW = (rect.width - gutter * (cols + 1)) / cols;
        const cellH = (rect.height - gutter * (rows + 1)) / rows;

        // Canonical full box = the currently-active (maximized) window's rendered
        // rect — used to grow a minimized window's collapsed bar back to full before
        // it's scaled down.
        const activeEl = desktop.getWindow(desktop.getActiveWindow())?.window;
        let box = (activeEl && !activeEl.classList.contains('min')) ? activeEl.getBoundingClientRect() : null;
        if (!box || !box.width || !box.height) box = rect;

        this._clearClickHandlers();
        ids.forEach((id, i) => {
            const winbox = desktop.getWindow(id);
            const el = winbox?.window;
            if (!el) return;

            // CRITICAL: never change the geometry of a window that's already shown
            // at full size — resizing a live xterm window is exactly what corrupts
            // its compositing layer (transparent background). We only ever SCALE it
            // via transform below, leaving its real size + .max class untouched.
            // Minimized windows are collapsed to a tiny bar, so those (and only
            // those) we grow to the full box first, while still hidden.
            if (el.classList.contains('min')) {
                el.classList.remove('min');
                winbox.min = false;
                el.style.left = Math.round(box.left) + 'px';
                el.style.top = Math.round(box.top) + 'px';
                el.style.width = Math.round(box.width) + 'px';
                el.style.height = Math.round(box.height) + 'px';
            }

            // Measure THIS window's actual rect, then place + shrink it into the
            // cell with a transform (cheap GPU op, no re-raster). Uniform scale +
            // centered = letterboxed thumbnail that lands exactly in its cell.
            const wb = el.getBoundingClientRect();
            if (!wb.width || !wb.height) return;
            const col = i % cols;
            const row = Math.floor(i / cols);
            const cellX = rect.left + gutter + col * (cellW + gutter);
            const cellY = rect.top + gutter + row * (cellH + gutter);
            const scale = Math.min(cellW / wb.width, cellH / wb.height);
            const drawW = wb.width * scale;
            const drawH = wb.height * scale;
            const tx = cellX + (cellW - drawW) / 2 - wb.left;
            const ty = cellY + (cellH - drawH) / 2 - wb.top;

            el.style.transformOrigin = 'top left';
            el.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
            el.style.zIndex = String(SCRIM_Z + 1 + i);

            // Capture-phase click anywhere in the window focuses it and exits.
            const fn = (e) => { e.stopPropagation(); this.exit(id); };
            el.addEventListener('click', fn, true);
            this._clickHandlers.push({ el, fn });
        });
    }

    /**
     * Re-run the layout against the current window set (handles mid-overlay churn).
     */
    _relayout() {
        if (!this._active) return;
        const ids = [...desktop.windows.keys()];
        if (ids.length < 2) { this.exit(); return; }
        this._layout(ids);
    }

    _onKeydown(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            this.exit();
        }
    }

    _clearClickHandlers() {
        this._clickHandlers.forEach(({ el, fn }) => el.removeEventListener('click', fn, true));
        this._clickHandlers = [];
    }

    _showScrim() {
        if (this._scrim) this._scrim.classList.remove('hidden');
    }

    _hideScrim() {
        if (this._scrim) this._scrim.classList.add('hidden');
    }
}

export const collage = new Collage();
