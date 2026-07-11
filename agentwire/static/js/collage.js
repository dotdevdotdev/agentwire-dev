/**
 * Collage — Mission Control overlay of live window previews.
 *
 * F3 (or the desktop_collage MCP tool, or the command palette) lays a preview
 * of every open window into a grid so the whole desktop can be scanned at once.
 * Click a tile to focus that window; Esc, F3, or clicking the backdrop exits.
 *
 * The tiles are NOT the real windows. Each tile is a lightweight live view:
 * session windows stream the same pane content as Monitor mode over their own
 * monitor WebSocket (/ws/{sessionId} — the server pushes the current screen on
 * connect and re-broadcasts on change); artifact windows render a cloned
 * iframe. The real WinBox windows are never moved, resized, transformed,
 * minimized, or restored by this module — entering and exiting the collage is
 * pure DOM addition/removal in an overlay layer.
 *
 * That constraint is the whole design. The previous implementation manipulated
 * the real windows (un-minimize, grow, transform-scale into a grid, restore on
 * exit) and was structurally unfixable:
 *   - faking `winbox.min` corrupted WinBox's internal min-stack, so any later
 *     minimize re-laid stale entries out as 250px-wide taskbar bars (the
 *     "windows shrink to thin bars" bug) and duplicated stack entries grew
 *     worse every enter/exit cycle;
 *   - WinBox animates left/top/width/height over 300ms, so any geometry read
 *     taken right after a write measured mid-transition garbage;
 *   - growing a hidden window to full size fired its ResizeObserver every
 *     animation frame → fitAddon.fit() + PTY resize spam → the real tmux
 *     session got resized and the xterm WebGL layer corrupted (transparent
 *     window backgrounds);
 *   - registerWindow/setActiveWindow enforce single-window mode (minimize all
 *     others), so any window event mid-overlay re-minimized the tiles
 *     (windows vanishing) while relayout fought to undo it.
 * A preview overlay sidesteps all of it: there is no state to restore, so
 * there is nothing to corrupt.
 *
 * @module collage
 */

import { wsProtocols } from './api.js';
import { desktop } from './desktop-manager.js';
import { ansiToHtml } from './utils/ansi.js';
import { isCommandPaletteOpen } from './command-palette.js';
import { getAllSessions, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { lineageTintVar, lineageOf } from './lineage.js';

/** Overlay z-index: above all windows (WinBox's focus counter sets inline
 * z-indexes that grow from 10), below notification toasts (1500), modals
 * (2000), the command palette (3000), and the sidebar (9001) — all of those
 * must stay usable on top of the collage. */
const OVERLAY_Z = 1400;

class Collage {
    constructor() {
        /** @type {boolean} */
        this._active = false;

        /** @type {HTMLElement|null} Backdrop + grid root (created in init) */
        this._overlay = null;

        /** @type {HTMLElement|null} Current grid element */
        this._grid = null;

        /** @type {Array<{ws: WebSocket|null}>} Per-tile live resources */
        this._tiles = [];

        /** @type {Array<Function>} desktop event unsubscribe fns (active only) */
        this._unsubs = [];

        /** @type {function(string): (object|null)} id → window instance lookup */
        this._lookup = () => null;

        // Family grouping (#748) needs session/parent data that only loads
        // once ensureSessionsLoaded() has resolved at least once (see there
        // for why getAllSessions() alone isn't reliable at cold start). Once
        // true, every subsequent build already has fresh data — no need to
        // re-trigger the one-time post-load rebuild in enter() again.
        this._sessionsReady = false;

        this._onKeydown = this._onKeydown.bind(this);
    }

    /**
     * Initialize. Creates the overlay element (hidden) inside the desktop area.
     * @param {function(string): (object|null)} [lookupInstance] - Resolves a
     *   window id to its SessionWindow/ArtifactWindow instance.
     */
    init(lookupInstance) {
        if (typeof lookupInstance === 'function') this._lookup = lookupInstance;
        const area = document.getElementById('desktopArea');
        if (!area) return;
        this._overlay = document.createElement('div');
        this._overlay.className = 'collage-overlay hidden';
        this._overlay.style.zIndex = String(OVERLAY_Z);
        // Backdrop click (the gaps between tiles) exits; tile clicks stop propagation.
        this._overlay.addEventListener('click', () => this.exit());
        area.appendChild(this._overlay);
    }

    /** @returns {boolean} Whether the overlay is currently up. */
    get active() {
        return this._active;
    }

    /**
     * Toggle the overlay.
     */
    toggle() {
        this._active ? this.exit() : this.enter();
    }

    /**
     * Enter the collage: build a live preview tile for every open window.
     */
    enter() {
        if (this._active || !this._overlay) return;
        const ids = [...desktop.windows.keys()];
        if (ids.length < 2) return;  // nothing to collage

        this._active = true;

        // Keystrokes would otherwise still reach the focused xterm <textarea>
        // behind the backdrop; exit() hands focus back to the active window.
        const ae = document.activeElement;
        if (ae && typeof ae.blur === 'function') ae.blur();

        this._overlay.classList.remove('hidden');
        this._buildGrid(ids);

        document.addEventListener('keydown', this._onKeydown, true);
        this._unsubs.push(
            // Window set changed underneath us — rebuild the previews.
            desktop.on('window_registered', () => this._rebuild()),
            desktop.on('window_unregistered', () => this._rebuild()),
            // Something else activated a window (Tab cycle, sidebar click, a
            // freshly-opened window): get out of the way and show it.
            desktop.on('active_window_changed', () => this._teardown()),
            // Grid geometry depends on the desktop area size.
            desktop.on('viewport_resize', () => this._rebuild()),
        );

        // First-ever open this page load: session/parent data may not have
        // loaded yet (see ensureSessionsLoaded), so the grid above may have
        // fallen back to ungrouped singletons. Rebuild once real data lands.
        // A no-op on every later open, since _sessionsReady is already true.
        if (!this._sessionsReady) {
            ensureSessionsLoaded().then(() => {
                this._sessionsReady = true;
                if (this._active) this._rebuild();
            });
        }
    }

    /**
     * Exit the collage.
     * @param {string|null} focusId - If given, that window becomes the active
     *   (maximized) window via the standard setActiveWindow path. Otherwise
     *   the desktop is exactly as it was — nothing to restore.
     */
    exit(focusId = null) {
        if (!this._active) return;
        this._teardown();

        if (focusId && desktop.windows.has(focusId)) {
            desktop.setActiveWindow(focusId);  // maximizes it, minimizes the rest
        }

        // Hand keyboard focus (back) to whichever window is now active.
        const inst = this._lookup(desktop.getActiveWindow());
        if (inst && typeof inst.focus === 'function') inst.focus();
    }

    // ============================================
    // Internals
    // ============================================

    /**
     * Remove the overlay chrome: subscriptions, key handler, tile sockets, grid
     * DOM. The real windows were never touched, so this is the entire exit path.
     */
    _teardown() {
        if (!this._active) return;
        this._active = false;
        document.removeEventListener('keydown', this._onKeydown, true);
        this._unsubs.forEach((fn) => { try { fn(); } catch (e) {} });
        this._unsubs = [];
        this._destroyTiles();
        this._overlay.classList.add('hidden');
    }

    /** Close every tile's WebSocket and drop the grid DOM. */
    _destroyTiles() {
        for (const tile of this._tiles) {
            if (tile.ws) {
                try {
                    tile.ws.onmessage = null;
                    tile.ws.onclose = null;
                    tile.ws.close();
                } catch (e) {}
            }
        }
        this._tiles = [];
        if (this._grid) {
            this._grid.remove();
            this._grid = null;
        }
    }

    /**
     * Rebuild the grid against the current window set (mid-overlay churn).
     */
    _rebuild() {
        if (!this._active) return;
        this._destroyTiles();
        const ids = [...desktop.windows.keys()];
        if (ids.length < 2) { this.exit(); return; }
        this._buildGrid(ids);
    }

    /**
     * Build the preview grid for the given window ids. Grid cells are
     * families (a session + its descendants), not raw windows — a family of
     * one renders as a plain tile, a family with children renders as a
     * tinted cluster with the parent on top and children nested below it
     * (#748). This keeps the aspect-fit cols×rows math (proven not to
     * overflow at 15+ tiles) exactly as it was, just computed over family
     * count instead of window count.
     * @param {string[]} ids
     */
    _buildGrid(ids) {
        const area = document.getElementById('desktopArea');
        if (!area) return;
        const areaRect = area.getBoundingClientRect();
        const families = this._groupFamilies(ids);
        const n = families.length;

        // Fit cols×rows to the desktop aspect so cells stay window-shaped.
        const aspect = areaRect.width / Math.max(1, areaRect.height);
        let cols = Math.max(1, Math.round(Math.sqrt(n * aspect)));
        cols = Math.min(cols, n);
        let rows = Math.ceil(n / cols);
        while (cols > 1 && (cols - 1) * rows >= n) cols--;
        rows = Math.ceil(n / cols);

        this._grid = document.createElement('div');
        this._grid.className = 'collage-grid';
        this._grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
        this._grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
        // Name-label size tracks cell height: big on a sparse grid, smaller (but
        // never tiny) on a dense one. Matches the .collage-card-label CSS var.
        const cellH = (areaRect.height - 18 * 2 - 14 * (rows - 1)) / rows;
        this._grid.style.setProperty(
            '--collage-label-size',
            `${Math.round(Math.min(30, Math.max(15, cellH * 0.09)))}px`,
        );

        const activeId = desktop.getActiveWindow();
        families.forEach((family) => {
            this._grid.appendChild(this._buildFamily(family.ids, family.root, activeId, areaRect));
        });
        this._overlay.appendChild(this._grid);

        // Scale each miniature into its tile body. Overlay elements have no
        // geometry transitions, so measuring right after append is exact.
        for (const mini of this._grid.querySelectorAll('.collage-mini')) {
            const body = mini.parentElement;
            const scale = Math.min(
                body.clientWidth / Math.max(1, areaRect.width),
                body.clientHeight / Math.max(1, areaRect.height),
                1,
            );
            mini.style.transform = `translate(-50%, -50%) scale(${scale})`;
        }
    }

    /**
     * Group open window ids into families keyed by session-tree root. The
     * root-resolution walk itself is lineage.js's `lineageOf` (#761 — the
     * same walk `groupFamilies` and `familyRootName` build on) rather than a
     * copy kept here; this method only adds the window-id-specific parts:
     * looking up each id's session name and giving non-session windows
     * (artifacts/panels) their own singleton family (rooted at their own
     * id, since they have no lineage to walk). The root name is threaded
     * through to _buildFamily so it can look up the family's hue via
     * lineage.js's lineageTintVar (#755) — the same assignment placement and
     * the shared topology renderer use, instead of re-deriving one here. Within a
     * family, ids are ordered ancestor-first so nested descendants render
     * under their parent even across multiple generations.
     * @param {string[]} ids
     * @returns {Array<{root: string, ids: string[]}>} One entry per family.
     */
    _groupFamilies(ids) {
        const byName = new Map(getAllSessions().map((s) => [s.name || '', s]));
        const families = new Map();  // root key → [{id, depth}]
        for (const id of ids) {
            const inst = this._lookup(id);
            const isSession = inst && typeof inst.session === 'string';
            const { root, depth } = isSession
                ? lineageOf(byName, inst.session)
                : { root: id, depth: 0 };
            if (!families.has(root)) families.set(root, []);
            families.get(root).push({ id, depth });
        }
        return [...families.entries()].map(([root, entries]) => ({
            root,
            ids: entries.sort((a, b) => a.depth - b.depth).map((e) => e.id),
        }));
    }

    /**
     * Build one grid cell for a family: a lone tile for a singleton family,
     * or a tinted cluster (parent on top, children nested below in a
     * wrapping row) for a family with descendants. Family hue comes from
     * lineage.js's `lineageTintVar` (#755) — a root-hash, not grid position,
     * so a family keeps the same hue here as on placement and the shared
     * topology renderer. `overflow: hidden` on the cluster (CSS) is what keeps a
     * family with many children from ever pushing the grid into horizontal
     * overflow — it scrolls vertically instead.
     * @param {string[]} familyIds - Ancestor-first window ids for this family.
     * @param {string} root - Family root session name (or window id, for a
     *   singleton family rooted at a non-session window).
     * @param {string|null} activeId - Currently-active window id.
     * @param {DOMRect} areaRect
     */
    _buildFamily(familyIds, root, activeId, areaRect) {
        const wrap = document.createElement('div');
        wrap.className = 'collage-family';
        wrap.style.setProperty('--family-tint', `var(${lineageTintVar(root, getAllSessions())})`);

        if (familyIds.length === 1) {
            wrap.classList.add('is-singleton');
            wrap.appendChild(this._buildTile(familyIds[0], familyIds[0] === activeId, areaRect));
            return wrap;
        }

        const [parentId, ...childIds] = familyIds;
        const parentWrap = document.createElement('div');
        parentWrap.className = 'collage-family-parent';
        parentWrap.appendChild(this._buildTile(parentId, parentId === activeId, areaRect));
        wrap.appendChild(parentWrap);

        const childrenWrap = document.createElement('div');
        childrenWrap.className = 'collage-family-children';
        for (const id of childIds) {
            const tile = this._buildTile(id, id === activeId, areaRect);
            tile.classList.add('is-child');
            childrenWrap.appendChild(tile);
        }
        wrap.appendChild(childrenWrap);

        return wrap;
    }

    /**
     * Build one preview tile: header (icon + window title) + live content.
     * @param {string} id - Window id
     * @param {boolean} isActive - Whether this is the currently-active window
     * @param {DOMRect} areaRect - Desktop area rect (miniatures render at this size)
     */
    _buildTile(id, isActive, areaRect) {
        const winbox = desktop.getWindow(id);
        const inst = this._lookup(id);

        const card = document.createElement('div');
        card.className = 'collage-card' + (isActive ? ' is-active' : '');
        card.addEventListener('click', (e) => {
            e.stopPropagation();
            this.exit(id);
        });

        const header = document.createElement('div');
        header.className = 'collage-card-header';
        // Reuse the window's existing titlebar icon (background-image on .wb-icon).
        const iconBg = winbox?.window?.querySelector('.wb-icon')?.style.backgroundImage;
        if (iconBg && iconBg !== 'none') {
            const icon = document.createElement('span');
            icon.className = 'collage-card-icon';
            icon.style.backgroundImage = iconBg;
            header.appendChild(icon);
        }
        const title = document.createElement('span');
        title.className = 'collage-card-title';
        title.textContent = (winbox && winbox.title) || id;
        header.appendChild(title);
        card.appendChild(header);

        const body = document.createElement('div');
        body.className = 'collage-card-body';
        card.appendChild(body);

        if (inst && typeof inst.session === 'string') {
            this._mountSessionPreview(body, inst, areaRect);
        } else if (inst && inst.iframe && inst.iframe.src) {
            this._mountArtifactPreview(body, inst, areaRect);
        } else {
            body.classList.add('collage-card-empty');
            body.textContent = 'no preview';
        }

        // Big centered name label — the primary identification when tiles are
        // small. Session name (no mode suffix) / artifact title; fades on hover
        // so the live content underneath stays inspectable.
        const label = document.createElement('div');
        label.className = 'collage-card-label';
        label.textContent =
            (inst && typeof inst.session === 'string') ? inst.sessionId
            : (inst && inst.title) ? inst.title
            : ((winbox && winbox.title) || id);
        body.appendChild(label);

        return card;
    }

    /** Fixed-size miniature canvas, centered + scaled into the tile body. */
    _makeMini(body, areaRect) {
        const mini = document.createElement('div');
        mini.className = 'collage-mini';
        mini.style.width = Math.round(areaRect.width) + 'px';
        mini.style.height = Math.round(areaRect.height) + 'px';
        body.appendChild(mini);
        return mini;
    }

    /**
     * Live session preview: a mini Monitor view over the session's monitor
     * WebSocket. The server pushes the current screen immediately on connect
     * and re-broadcasts whenever the pane content changes.
     */
    _mountSessionPreview(body, inst, areaRect) {
        const mini = this._makeMini(body, areaRect);
        const pre = document.createElement('pre');
        pre.className = 'collage-mini-pre';
        mini.appendChild(pre);

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        let ws = null;
        try {
            ws = new WebSocket(`${protocol}//${location.host}/ws/${inst.sessionId}`, wsProtocols());
        } catch (e) {
            return;
        }
        ws.onmessage = (event) => {
            let msg;
            try { msg = JSON.parse(event.data); } catch (e) { return; }
            if (msg.type === 'output' && msg.data) {
                pre.innerHTML = ansiToHtml(msg.data);
                // Bottom-anchor: the tail of the capture is the live screen.
                mini.scrollTop = mini.scrollHeight;
            } else if (msg.type === 'audio' && msg.data) {
                // Same behavior as an open Monitor window — play session audio.
                // desktop._playAudio dedupes at the device level, so a real
                // window attached to the same session won't double-play.
                desktop._playAudio(msg.data, inst.sessionId);
            }
        };
        this._tiles.push({ ws });
    }

    /** Live artifact preview: a cloned iframe at desktop size, scaled down. */
    _mountArtifactPreview(body, inst, areaRect) {
        const mini = this._makeMini(body, areaRect);
        const iframe = document.createElement('iframe');
        iframe.className = 'collage-mini-iframe';
        const sandbox = inst.iframe.getAttribute('sandbox');
        if (sandbox) iframe.setAttribute('sandbox', sandbox);
        iframe.src = inst.iframe.src;
        mini.appendChild(iframe);
        this._tiles.push({ ws: null });
    }

    _onKeydown(e) {
        if (e.key === 'Escape' && !isCommandPaletteOpen()) {
            e.preventDefault();
            e.stopPropagation();
            this.exit();
        }
    }
}

export const collage = new Collage();
