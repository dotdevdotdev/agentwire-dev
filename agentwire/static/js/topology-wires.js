/**
 * Topology wires — read-only SVG connector overlay from each parent session's
 * title bar to each live child's title bar (#746).
 *
 * Strictly read-only, same discipline as collage.js: it only *reads* window
 * geometry via getBoundingClientRect to anchor wires, and never writes to a
 * real WinBox window (no move/resize/minimize/restore). The overlay is a
 * plain SVG layered into the same #desktopArea collage.js appends into.
 *
 * Parent/child pairing reuses sessions-section.js's `s.parent` linkage (the
 * same data the sidebar's nested tree renders from) rather than re-deriving
 * it — see ensureSessionsLoaded()/getAllSessions()/activityStates there
 * (also used by collage.js's #748 family grouping). Colors come from
 * lineage.js's `lineageTintVar` (#755 — the same family → hue assignment
 * placement and collage use) plus the failure-state-parity tokens (#749) in
 * desktop.css; never a hardcoded hex.
 *
 * @module topology-wires
 */

import { desktop } from './desktop-manager.js';
import { buildSessionId, normalizeMachine } from './session-id.js';
import { getAllSessions, activityStates, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { lineageTintVar } from './lineage.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Overlay z-index: above WinBox windows (inline z-indexes grow from 10),
 * below the collage overlay (1400 — see collage.js) so entering collage
 * always wins. */
const WIRES_Z = 900;

const VISIBLE_KEY = 'aw-topology-wires-visible';
export const TOPOLOGY_WIRES_EVENT = 'topology-wires-change';

/** 'idle' | 'flow' (processing/generating/playing) | 'awaiting' | 'stuck'.
 * `state`/`state_kind` (needs_input/off) only land on the session record
 * right after an /api/sessions/local fetch — not on every periodic
 * sessions_update push — so treat them as a best-effort overlay on top of
 * the always-live activityStates map (same source the sidebar dot uses). */
function wireStateFor(name, record) {
    if (record?.state === 'needs_input') return 'awaiting';
    if (record?.state === 'off') return 'stuck';
    const activity = activityStates.get(name) || record?.activity || 'idle';
    if (activity === 'processing' || activity === 'generating' || activity === 'playing' || activity === 'active') {
        return 'flow';
    }
    return 'idle';
}

/**
 * A minimized WinBox is `display: none` (this app hides WinBox's own min-bar
 * stack in favor of the sidebar's taskbar list — desktop.css `.winbox.min`),
 * which collapses getBoundingClientRect to an all-zero rect rather than some
 * corner position. Treat that as "no anchor" so the pair's wire is dropped
 * instead of hanging from the desktop's top-left corner; it reappears the
 * next tick after restore, which is what "redraw on minimize/restore" means
 * for a window that currently has nowhere sensible to point to.
 */
function headerRect(winbox) {
    const el = winbox && winbox.window;
    if (!el || winbox.min) return null;
    const header = el.querySelector('.wb-header') || el;
    const rect = header.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return null;
    return rect;
}

/** Vertical S-curve hanging from each header's bottom edge — reads sensibly
 * whether the two windows are tiled side by side or stacked. */
function bezierPath(x1, y1, x2, y2) {
    const bend = Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1), 20) * 0.4;
    return `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 + bend}, ${x2} ${y2}`;
}

class TopologyWires {
    constructor() {
        /** @type {SVGSVGElement|null} */
        this._svg = null;
        /** @type {Map<string, {path: SVGPathElement, stateClass: string|null, tintVar: string|null}>} */
        this._paths = new Map();
        /** @type {number|null} */
        this._raf = null;
        /** @type {boolean} */
        this._hasPairs = false;
        this._visible = localStorage.getItem(VISIBLE_KEY) !== '0';

        this._tick = this._tick.bind(this);
    }

    get visible() {
        return this._visible;
    }

    /** Toggle. Alt+L in desktop.js, and the Config sidebar checkbox, both call this. */
    toggle() {
        this.setVisible(!this._visible);
    }

    setVisible(v) {
        this._visible = v;
        if (v) localStorage.removeItem(VISIBLE_KEY);
        else localStorage.setItem(VISIBLE_KEY, '0');
        this._svg?.classList.toggle('hidden', !v);
        window.dispatchEvent(new CustomEvent(TOPOLOGY_WIRES_EVENT));
        if (v) this._wake();
    }

    /**
     * Create the overlay SVG inside #desktopArea and start listening for
     * anything that could move a title bar. Call once from desktop.js init.
     */
    init() {
        const area = document.getElementById('desktopArea');
        if (!area) return;

        // The shared session/activity pipeline (sessions-section.js) only goes
        // live once the Sessions sidebar accordion is expanded — ensure it's
        // live regardless, so wires render on a fresh page load. Same helper
        // collage.js's #748 family grouping uses; memoized there so this
        // isn't a second parallel fetch.
        ensureSessionsLoaded();

        this._svg = document.createElementNS(SVG_NS, 'svg');
        this._svg.setAttribute('class', 'topology-wires-overlay' + (this._visible ? '' : ' hidden'));
        this._svg.style.zIndex = String(WIRES_Z);
        area.appendChild(this._svg);

        const wake = () => this._wake();
        onSessionsChanged(wake);
        desktop.on('window_registered', wake);
        desktop.on('window_unregistered', wake);
        desktop.on('window_minimized', wake);
        desktop.on('window_restored', wake);
        desktop.on('window_tiled', wake);
        desktop.on('viewport_resize', wake);
        desktop.on('session_activity', wake);
        desktop.on('tts_start', wake);
        desktop.on('audio', wake);
        desktop.on('audio_ended', wake);

        this._wake();
    }

    /** Kick the redraw loop if it isn't already running. Self-stops once
     * there's nothing to draw, so it doesn't spin forever in the common
     * single-session case. */
    _wake() {
        if (!this._visible || this._raf !== null) return;
        this._raf = requestAnimationFrame(this._tick);
    }

    _tick() {
        this._raf = null;
        this._redraw();
        if (this._visible && this._hasPairs) {
            this._raf = requestAnimationFrame(this._tick);
        }
    }

    _computePairs() {
        const sessions = getAllSessions();
        const byName = new Map(sessions.map((s) => [s.name || '', s]));
        const pairs = [];
        for (const s of sessions) {
            const name = s.name || '';
            const parentName = s.parent;
            if (!name || !parentName || parentName === name || !byName.has(parentName)) continue;
            const parentRec = byName.get(parentName);
            const childId = buildSessionId(name, normalizeMachine(s.machine));
            const parentId = buildSessionId(parentName, normalizeMachine(parentRec.machine));
            const parentWin = desktop.getWindow(parentId);
            const childWin = desktop.getWindow(childId);
            if (!parentWin || !childWin) continue; // only wire up live (open) windows on both ends

            pairs.push({
                key: `${parentId}=>${childId}`,
                parentWin,
                childWin,
                tintVar: lineageTintVar(name, sessions),
                state: wireStateFor(name, s),
            });
        }
        return pairs;
    }

    _redraw() {
        const area = document.getElementById('desktopArea');
        if (!area || !this._svg) return;
        const areaRect = area.getBoundingClientRect();
        const pairs = this._computePairs();

        const seen = new Set();
        for (const pair of pairs) {
            const parentRect = headerRect(pair.parentWin);
            const childRect = headerRect(pair.childWin);
            if (!parentRect || !childRect) continue;
            seen.add(pair.key);

            const x1 = parentRect.left + parentRect.width / 2 - areaRect.left;
            const y1 = parentRect.bottom - areaRect.top;
            const x2 = childRect.left + childRect.width / 2 - areaRect.left;
            const y2 = childRect.bottom - areaRect.top;
            const d = bezierPath(x1, y1, x2, y2);

            let entry = this._paths.get(pair.key);
            if (!entry) {
                const path = document.createElementNS(SVG_NS, 'path');
                path.setAttribute('class', 'topology-wire');
                this._svg.appendChild(path);
                entry = { path, stateClass: null, tintVar: null };
                this._paths.set(pair.key, entry);
            }
            if (entry.path.getAttribute('d') !== d) entry.path.setAttribute('d', d);

            const stateClass = pair.state === 'idle' ? null : `topology-wire--${pair.state}`;
            if (entry.stateClass !== stateClass) {
                if (entry.stateClass) entry.path.classList.remove(entry.stateClass);
                if (stateClass) entry.path.classList.add(stateClass);
                entry.stateClass = stateClass;
            }
            if (entry.tintVar !== pair.tintVar) {
                entry.path.style.setProperty('--wire-tint', `var(${pair.tintVar})`);
                entry.tintVar = pair.tintVar;
            }
        }

        for (const [key, entry] of this._paths) {
            if (!seen.has(key)) {
                entry.path.remove();
                this._paths.delete(key);
            }
        }

        // Drives whether _tick keeps looping: at least one wire actually on
        // screen right now (not just logically paired — e.g. both minimized
        // in this app's default single-window mode counts as nothing to
        // follow). window_restored/_tiled events re-wake the loop later.
        this._hasPairs = seen.size > 0;
    }
}

export const topologyWires = new TopologyWires();
