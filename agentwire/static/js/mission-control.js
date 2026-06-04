/**
 * Mission Control - window collage overlay.
 *
 * Lays every open window into a grid so the whole desktop can be scanned at once.
 * Click any tile to focus that window and exit; Esc exits restoring the prior state.
 *
 * The portal runs in single-window mode (one maximized window, the rest minimized),
 * so "prior state" is just: which window was active + each window's minimized flag +
 * any tile zones. Live windows act as the tiles — no snapshots, no scaling. Grid
 * placement reuses tileManager.layoutGrid (same move/resize/refit pipeline as tiling).
 *
 * @module mission-control
 */

import { desktop } from './desktop-manager.js';
import { tileManager } from './tile-manager.js';

/** z-index band: scrim sits at BASE, grid windows above it. */
const SCRIM_Z = 5000;

class MissionControl {
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

        this._onKeydown = this._onKeydown.bind(this);
    }

    /**
     * Initialize. Creates the scrim element (hidden) inside the desktop area.
     */
    init() {
        const area = document.getElementById('desktopArea');
        if (!area) return;
        this._scrim = document.createElement('div');
        this._scrim.className = 'mission-control-overlay hidden';
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
     * Enter Mission Control: restore every window and lay them into a grid.
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

        // Clear tile states BEFORE restoring — otherwise tile-manager's
        // window_restored handler re-tiles each window to its old zone, fighting
        // the grid. The snapshot above lets us put them back on exit.
        desktop.tileStates.clear();

        // Restore every minimized window so it can take a grid slot.
        for (const [id, winbox] of desktop.windows) {
            if (winbox && winbox.min) {
                desktop._safeWinBoxOp(winbox, 'restore', id);
            }
        }

        this._active = true;
        this._showScrim();
        this._layout(ids);

        document.addEventListener('keydown', this._onKeydown, true);
        this._unsubs.push(
            desktop.on('window_registered', () => this._relayout()),
            desktop.on('window_unregistered', () => this._relayout()),
            desktop.on('session_closed', () => this._relayout()),
        );
    }

    /**
     * Exit Mission Control.
     * @param {string|null} focusId - If given, that window becomes the single
     *   maximized window (click-to-focus). Otherwise the pre-collage state restores.
     */
    exit(focusId = null) {
        if (!this._active) return;
        this._active = false;

        document.removeEventListener('keydown', this._onKeydown, true);
        this._unsubs.forEach((fn) => { try { fn(); } catch (e) {} });
        this._unsubs = [];
        this._clearClickHandlers();
        this._hideScrim();

        // Drop transient grid styling/z-index from every window.
        for (const [, winbox] of desktop.windows) {
            if (winbox?.window) {
                winbox.window.classList.remove('tiled');
                winbox.window.style.zIndex = '';
            }
        }

        const snap = this._snapshot;
        this._snapshot = null;

        if (focusId && desktop.windows.has(focusId)) {
            // Commit to the clicked window: single-window mode, rest minimized.
            desktop.setActiveWindow(focusId);
            return;
        }

        if (!snap) return;

        // Esc: restore exact prior state — re-tile what was tiled, then re-maximize
        // the prior active window (setActiveWindow minimizes all non-tiled others).
        for (const [id, zone] of snap.tileStates) {
            if (desktop.windows.has(id)) tileManager._tileWindow(id, zone);
        }
        if (snap.activeId && desktop.windows.has(snap.activeId) && !desktop.tileStates.has(snap.activeId)) {
            desktop.setActiveWindow(snap.activeId);
        } else if (desktop.tileStates.size === 0) {
            desktop.minimizeAllExcept(null);
        }
    }

    // ============================================
    // Internals
    // ============================================

    /**
     * Lay the given windows into the grid and (re)wire click-to-focus + z-index.
     * @param {string[]} ids
     */
    _layout(ids) {
        tileManager.layoutGrid(ids);
        this._clearClickHandlers();
        ids.forEach((id, i) => {
            const winbox = desktop.getWindow(id);
            if (!winbox?.window) return;
            // Stack above the scrim.
            winbox.window.style.zIndex = String(SCRIM_Z + 1 + i);
            // Capture-phase click anywhere in the window focuses it and exits.
            const fn = (e) => { e.stopPropagation(); this.exit(id); };
            winbox.window.addEventListener('click', fn, true);
            this._clickHandlers.push({ el: winbox.window, fn });
        });
    }

    /**
     * Re-run the layout against the current window set (handles mid-overlay churn).
     */
    _relayout() {
        if (!this._active) return;
        const ids = [...desktop.windows.keys()];
        if (ids.length < 2) { this.exit(); return; }
        // A newly-registered window may have minimized the others (registerWindow
        // behavior) — restore everything before re-gridding.
        for (const [id, winbox] of desktop.windows) {
            if (winbox && winbox.min) desktop._safeWinBoxOp(winbox, 'restore', id);
        }
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

export const missionControl = new MissionControl();
