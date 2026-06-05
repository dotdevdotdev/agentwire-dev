/**
 * Tile Manager - Drag-to-tile window management
 *
 * Enables dragging window title bars to snap windows into
 * half-screen or quadrant positions. Maximized remains the default;
 * tiling is opt-in via drag gesture.
 *
 * @module tile-manager
 */

import { desktop } from './desktop-manager.js';

/** Zone definitions as fractions of the desktop area */
const ZONES = {
    'left':         { top: 0, left: 0, width: 0.5, height: 1 },
    'right':        { top: 0, left: 0.5, width: 0.5, height: 1 },
    'top':          { top: 0, left: 0, width: 1, height: 0.5 },
    'bottom':       { top: 0.5, left: 0, width: 1, height: 0.5 },
    'top-left':     { top: 0, left: 0, width: 0.5, height: 0.5 },
    'top-right':    { top: 0, left: 0.5, width: 0.5, height: 0.5 },
    'bottom-left':  { top: 0.5, left: 0, width: 0.5, height: 0.5 },
    'bottom-right': { top: 0.5, left: 0.5, width: 0.5, height: 0.5 },
};

/** Pixels from edge to trigger half-zone */
const EDGE_THRESHOLD = 48;

/** Pixels of mouse movement before activating drag */
const DRAG_THRESHOLD = 8;

class TileManager {
    constructor() {
        /** @type {HTMLElement|null} */
        this.desktopArea = null;

        /** @type {HTMLElement|null} */
        this.overlay = null;

        /** @type {HTMLElement|null} */
        this.preview = null;

        /** @type {string|null} Current zone during drag */
        this._currentZone = null;

        /** @type {string|null} Window ID being dragged */
        this._dragId = null;

        /** @type {{x: number, y: number}|null} Mouse start position */
        this._dragStart = null;

        /** @type {boolean} Whether drag threshold has been exceeded */
        this._dragActive = false;

        // Bind handlers so we can add/remove them
        this._onMouseMove = this._onMouseMove.bind(this);
        this._onMouseUp = this._onMouseUp.bind(this);
    }

    /**
     * Initialize the tile manager.
     * Call after DOM is ready and desktop manager is connected.
     */
    init() {
        this.desktopArea = document.getElementById('desktopArea');
        if (!this.desktopArea) return;

        this._createOverlay();

        // Attach drag handler to newly registered windows
        desktop.on('window_registered', ({ id, winbox }) => {
            this._attachDragHandler(id, winbox);
        });

        // Clean up tile state when windows are unregistered
        desktop.on('window_unregistered', ({ id }) => {
            desktop.tileStates.delete(id);
        });

        // Re-apply tile position when a window is restored from minimize
        desktop.on('window_restored', ({ id }) => {
            const zone = desktop.tileStates.get(id);
            if (zone) {
                // Small delay to let WinBox finish its restore animation
                requestAnimationFrame(() => this._tileWindow(id, zone));
            }
        });

        // Re-tile tiled windows when the viewport resizes
        desktop.on('viewport_resize', () => {
            for (const [id, zone] of desktop.tileStates) {
                this._tileWindow(id, zone);
            }
        });

        // Re-apply tile positions on browser resize
        window.addEventListener('resize', () => {
            for (const [id, zone] of desktop.tileStates) {
                this._tileWindow(id, zone);
            }
        });
    }

    /**
     * Create the overlay element with preview highlight.
     */
    _createOverlay() {
        this.overlay = document.createElement('div');
        this.overlay.className = 'tile-overlay hidden';

        this.preview = document.createElement('div');
        this.preview.className = 'tile-preview';
        this.overlay.appendChild(this.preview);

        document.body.appendChild(this.overlay);
    }

    /**
     * Attach a drag handler to a window's title bar.
     *
     * Uses capture phase on the WinBox outer element so our handler fires
     * BEFORE WinBox's internal drag handler (which calls stopPropagation).
     *
     * @param {string} id - Window identifier
     * @param {WinBox} winbox - WinBox instance
     */
    _attachDragHandler(id, winbox) {
        if (!winbox || !winbox.window) return;

        winbox.window.addEventListener('mousedown', (e) => {
            // Only handle clicks on the header area
            if (!e.target.closest('.wb-header')) return;
            // Skip control buttons
            if (e.target.closest('.wb-close, .wb-min, .wb-max, .wb-full')) return;
            // Only left mouse button
            if (e.button !== 0) return;

            this._dragId = id;
            this._dragStart = { x: e.clientX, y: e.clientY };
            this._dragActive = false;

            // Focus the window (since we're stopping WinBox's handler)
            winbox.focus();

            document.addEventListener('mousemove', this._onMouseMove);
            document.addEventListener('mouseup', this._onMouseUp);

            // Stop WinBox's internal drag handler from firing
            e.stopPropagation();
            e.preventDefault();
        }, true);  // capture phase — fires before WinBox's bubble-phase handler
    }

    /**
     * Handle mouse movement during drag.
     * @param {MouseEvent} e
     */
    _onMouseMove(e) {
        if (!this._dragStart) return;

        const dx = e.clientX - this._dragStart.x;
        const dy = e.clientY - this._dragStart.y;

        // Check drag threshold
        if (!this._dragActive) {
            if (Math.sqrt(dx * dx + dy * dy) < DRAG_THRESHOLD) return;
            this._dragActive = true;
            this._showOverlay();
        }

        // Determine which zone the cursor is in
        const zone = this._hitTestZone(e.clientX, e.clientY);
        if (zone !== this._currentZone) {
            this._currentZone = zone;
            this._updatePreview(zone);
        }
    }

    /**
     * Handle mouse release — tile or re-maximize.
     * @param {MouseEvent} e
     */
    _onMouseUp(e) {
        document.removeEventListener('mousemove', this._onMouseMove);
        document.removeEventListener('mouseup', this._onMouseUp);

        const id = this._dragId;
        const wasActive = this._dragActive;
        const zone = this._currentZone;

        this._dragId = null;
        this._dragStart = null;
        this._dragActive = false;
        this._currentZone = null;
        this._hideOverlay();

        if (!wasActive || !id) return;

        if (zone) {
            this._tileWindow(id, zone);
        } else {
            this._maximizeWindow(id);
        }
    }

    /**
     * Determine which tile zone the cursor is in.
     * @param {number} clientX
     * @param {number} clientY
     * @returns {string|null} Zone name or null for center/maximize
     */
    _hitTestZone(clientX, clientY) {
        const rect = this.desktopArea.getBoundingClientRect();

        const x = clientX - rect.left;
        const y = clientY - rect.top;
        const w = rect.width;
        const h = rect.height;

        // Center dead zone (30% x 30%) — re-maximize
        const cx = w * 0.35;
        const cy = h * 0.35;
        const cw = w * 0.3;
        const ch = h * 0.3;
        if (x >= cx && x <= cx + cw && y >= cy && y <= cy + ch) {
            return null;
        }

        const nearLeft = x < EDGE_THRESHOLD;
        const nearRight = x > w - EDGE_THRESHOLD;
        const nearTop = y < EDGE_THRESHOLD;
        const nearBottom = y > h - EDGE_THRESHOLD;

        // Corners (two edges simultaneously)
        if (nearTop && nearLeft) return 'top-left';
        if (nearTop && nearRight) return 'top-right';
        if (nearBottom && nearLeft) return 'bottom-left';
        if (nearBottom && nearRight) return 'bottom-right';

        // Edges (half zones)
        if (nearLeft) return 'left';
        if (nearRight) return 'right';
        if (nearTop) return 'top';
        if (nearBottom) return 'bottom';

        // Fallback: quadrant based on which half the cursor is in
        const inLeft = x < w / 2;
        const inTop = y < h / 2;
        if (inTop && inLeft) return 'top-left';
        if (inTop && !inLeft) return 'top-right';
        if (!inTop && inLeft) return 'bottom-left';
        return 'bottom-right';
    }

    /**
     * Tile a window to a specific zone.
     * @param {string} id - Window identifier
     * @param {string} zone - Zone name from ZONES
     */
    _tileWindow(id, zone) {
        const winbox = desktop.getWindow(id);
        if (!winbox) return;

        const zoneDef = ZONES[zone];
        if (!zoneDef) return;

        const rect = this.desktopArea.getBoundingClientRect();

        // Remove .max class and flag — WinBox's .max CSS uses !important
        // which overrides programmatic move/resize
        winbox.window.classList.remove('max');
        winbox.max = false;

        // Add .tiled class
        winbox.window.classList.add('tiled');

        // Calculate pixel positions
        const x = rect.left + zoneDef.left * rect.width;
        const y = rect.top + zoneDef.top * rect.height;
        const w = zoneDef.width * rect.width;
        const h = zoneDef.height * rect.height;

        winbox.move(x, y);
        winbox.resize(w, h);

        // Store tile state
        desktop.tileStates.set(id, zone);

        // Emit event for terminal resize etc.
        desktop.emit('window_tiled', { id, zone });
    }

    /**
     * Lay a set of windows out into a grid collage across the desktop area.
     *
     * Reuses the same move/resize mechanics as _tileWindow (strip .max, position
     * via winbox.move/resize, emit window_tiled so terminals refit) but for an
     * arbitrary N-cell grid. Unlike tiling, this is a transient view: it does NOT
     * write desktop.tileStates, so nothing persists or re-applies on restore.
     *
     * @param {string[]} ids - Window identifiers to place, in grid order
     * @param {number} [gutter=8] - Pixel gap between tiles and around the edges
     */
    layoutGrid(ids, gutter = 8) {
        if (!this.desktopArea || !ids.length) return;

        const rect = this.desktopArea.getBoundingClientRect();
        const n = ids.length;

        // Fit cols×rows to the desktop aspect ratio so tiles stay close to the
        // viewport shape rather than squashed. cols = ceil(sqrt(n * aspect)).
        const aspect = rect.width / rect.height;
        let cols = Math.max(1, Math.round(Math.sqrt(n * aspect)));
        cols = Math.min(cols, n);
        let rows = Math.ceil(n / cols);
        // Avoid a trailing near-empty row when one more column packs it tighter.
        while (cols > 1 && (cols - 1) * rows >= n) cols--;
        rows = Math.ceil(n / cols);

        const cellW = (rect.width - gutter * (cols + 1)) / cols;
        const cellH = (rect.height - gutter * (rows + 1)) / rows;

        ids.forEach((id, i) => {
            const winbox = desktop.getWindow(id);
            if (!winbox || !winbox.window) return;

            const col = i % cols;
            const row = Math.floor(i / cols);
            const x = rect.left + gutter + col * (cellW + gutter);
            const y = rect.top + gutter + row * (cellH + gutter);

            // Un-hide and de-maximize: `.winbox.min` is `display:none !important`
            // (minimized windows), and `.max` uses !important geometry — both
            // override move/resize, so a window would stay hidden/maximized in
            // its grid cell. Clear the classes AND the flags (kept in sync so a
            // later minimizeAllExcept still re-minimizes correctly).
            winbox.window.classList.remove('min', 'max');
            winbox.min = false;
            winbox.max = false;
            // Reuse .tiled for the smooth left/top/width/height transition.
            winbox.window.classList.add('tiled');

            winbox.move(x, y);
            winbox.resize(cellW, cellH);

            // Trigger terminal refit after the transition (same wiring as tiling).
            desktop.emit('window_tiled', { id });
        });
    }

    /**
     * Re-maximize a window (remove tiling).
     * @param {string} id - Window identifier
     */
    _maximizeWindow(id) {
        const winbox = desktop.getWindow(id);
        if (!winbox) return;

        winbox.window.classList.remove('tiled');
        desktop.tileStates.delete(id);

        winbox.maximize();
    }

    /**
     * Show the tile overlay.
     */
    _showOverlay() {
        if (this.overlay) {
            this.overlay.classList.remove('hidden');
        }
    }

    /**
     * Hide the tile overlay and reset preview.
     */
    _hideOverlay() {
        if (this.overlay) {
            this.overlay.classList.add('hidden');
        }
        if (this.preview) {
            this.preview.style.display = 'none';
        }
    }

    /**
     * Update the preview highlight to show the target zone.
     * @param {string|null} zone - Zone name or null
     */
    _updatePreview(zone) {
        if (!this.preview) return;

        if (!zone) {
            this.preview.style.display = 'none';
            return;
        }

        const zoneDef = ZONES[zone];
        if (!zoneDef) {
            this.preview.style.display = 'none';
            return;
        }

        this.preview.style.display = 'block';
        this.preview.style.top = `${zoneDef.top * 100}%`;
        this.preview.style.left = `${zoneDef.left * 100}%`;
        this.preview.style.width = `${zoneDef.width * 100}%`;
        this.preview.style.height = `${zoneDef.height * 100}%`;
    }
}

export const tileManager = new TileManager();
