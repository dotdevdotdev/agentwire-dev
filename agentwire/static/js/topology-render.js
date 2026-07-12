/**
 * topology-render.js
 *
 * Mount-agnostic session-family renderer (#761) — the shared engine behind
 * the Session Workspace window (#762) and the phantom overlay (#764).
 * Given a container element and a session list, TopologyView renders one
 * block per family (root + descendants, grouped by lineage.js's
 * `groupFamilies`): a card per session (status dot, name, role chip,
 * activity sparkline, machine tag) plus curved SVG links from each card to
 * its parent's, tinted by the family's `lineageTintVar`. `render()` is
 * idempotent — repeat calls diff cards/rows/links against the previous pass
 * instead of tearing down and rebuilding the DOM, so a spawn or kill
 * mid-view doesn't flash the whole tree.
 *
 * Deliberately narrow-first: cards lay out in normal document flow
 * (flex-wrap rows, 1-2 cards per row), never an absolutely-positioned wide
 * canvas — the owner runs the portal in a narrow ~1/3-width window, which is
 * exactly why the connector overlay this module superseded (#746, wiring
 * title bars of spread-out windows — deleted by #764) read as a stray line
 * slashing across terminal text. `wireStateFor` below is the one shared
 * status mapping so a card and the sidebar dot never disagree on what
 * "awaiting"/"stuck" means.
 *
 * @module topology-render
 */

import { groupFamilies, lineageTintVar } from './lineage.js';
import { activityStates } from './sidebar/sessions-section.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * 'idle' | 'flow' (processing/generating/playing) | 'awaiting' | 'stuck'.
 * `state`/`state_kind` (needs_input/off) only land on the session record
 * right after an /api/sessions/local fetch — not on every periodic
 * sessions_update push — so treat them as a best-effort overlay on top of
 * the always-live activityStates map (same source the sidebar dot uses).
 *
 * @param {string} name - Session name.
 * @param {{state?: string, activity?: string}} [record] - Session record.
 * @returns {'idle'|'flow'|'awaiting'|'stuck'}
 */
export function wireStateFor(name, record) {
    if (record?.state === 'needs_input') return 'awaiting';
    if (record?.state === 'off') return 'stuck';
    const activity = activityStates.get(name) || record?.activity || 'idle';
    if (activity === 'processing' || activity === 'generating' || activity === 'playing' || activity === 'active') {
        return 'flow';
    }
    return 'idle';
}

/** Vertical S-curve from a parent card's bottom edge to a child card's top
 * edge — reads sensibly whether the pair ends up side by side or stacked
 * across a row wrap. */
function bezierPath(x1, y1, x2, y2) {
    const bend = Math.max(Math.abs(y2 - y1), 24) * 0.5;
    return `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`;
}

export class TopologyView {
    /**
     * @param {HTMLElement} container - Mount point; TopologyView owns everything appended under it.
     * @param {object} [opts]
     * @param {(name: string, session: object, slotEl: HTMLElement) => (() => void)|void} [opts.onCardExpand] -
     *   Fired when a card is clicked and expands inline. Receives the session name, record, and an
     *   empty slot element appended into the card — mount whatever content belongs there (e.g. a
     *   mini-terminal) and optionally return a cleanup function. TopologyView calls that cleanup on
     *   collapse (re-click), when the card is pruned (session disappeared), or on dispose(). Omitting
     *   this makes cards inert (e.g. the non-interactive phantom overlay).
     * @param {boolean} [opts.showLinks=true] - Draw the connector SVG layer.
     * @param {'window'|'overlay'} [opts.mode='window'] - Styling hook only — 'overlay' renders
     *   translucent glass cards for popping over a live terminal window; 'window' (default)
     *   renders solid chrome for a first-class workspace window.
     */
    constructor(container, opts = {}) {
        this._container = container;
        this._onCardExpand = opts.onCardExpand || null;
        this._showLinks = opts.showLinks !== false;
        this._mode = opts.mode === 'overlay' ? 'overlay' : 'window';
        this._lastSessions = [];
        /** @type {string|null} name of the currently expanded card, if any (accordion — one at a time) */
        this._expandedCard = null;

        /** @type {Map<string, {familyEl: HTMLElement, rows: Map<number, HTMLElement>}>} */
        this._families = new Map();
        /** @type {Map<string, object>} card entries keyed by session name */
        this._cards = new Map();
        /** @type {Map<string, {path: SVGPathElement, stateClass: string|null, tintVar: string|null}>} */
        this._links = new Map();
        this._raf = null;

        this._root = document.createElement('div');
        this._root.className = `topology-view topology-view--${this._mode}`;
        container.appendChild(this._root);

        if (this._showLinks) {
            this._svg = document.createElementNS(SVG_NS, 'svg');
            this._svg.setAttribute('class', 'topology-view-links');
            this._root.appendChild(this._svg);
        } else {
            this._svg = null;
        }

        this._scheduleRedraw = this._scheduleRedraw.bind(this);
        this._resizeObserver = new ResizeObserver(this._scheduleRedraw);
        this._resizeObserver.observe(this._root);
        window.addEventListener('resize', this._scheduleRedraw);
    }

    /**
     * Render (or re-render) the family tree for the given session list.
     * Idempotent: existing family/row/card DOM nodes are reused and only
     * their content/classes are patched, so repeated calls diff in place.
     * @param {Array<object>} sessions
     */
    render(sessions) {
        this._lastSessions = sessions || [];
        const byName = new Map(this._lastSessions.map((s) => [s.name || '', s]));
        const families = groupFamilies(this._lastSessions);

        const seenFamilies = new Set();
        const seenCards = new Set();

        for (const family of families) {
            seenFamilies.add(family.root);
            const entry = this._ensureFamily(family.root);
            const seenRows = new Set();
            for (const member of family.members) {
                const session = byName.get(member.name);
                if (!session) continue;
                seenCards.add(member.name);
                seenRows.add(member.depth);
                const row = this._ensureRow(entry, member.depth);
                this._renderCard(row, session, family.root);
            }
            this._pruneRows(entry, seenRows);
        }

        this._pruneFamilies(seenFamilies);
        this._pruneCards(seenCards);
        this._scheduleRedraw();
    }

    /** Tear down everything TopologyView appended into its container. */
    dispose() {
        if (this._expandedCard) this._collapseCard(this._expandedCard);
        this._resizeObserver.disconnect();
        window.removeEventListener('resize', this._scheduleRedraw);
        if (this._raf !== null) cancelAnimationFrame(this._raf);
        this._root.remove();
        this._families.clear();
        this._cards.clear();
        this._links.clear();
    }

    _ensureFamily(root) {
        let entry = this._families.get(root);
        if (!entry) {
            const familyEl = document.createElement('div');
            familyEl.className = 'topology-family';
            familyEl.dataset.familyRoot = root;
            this._root.appendChild(familyEl);
            entry = { familyEl, rows: new Map() };
            this._families.set(root, entry);
        }
        entry.familyEl.style.setProperty('--family-tint', `var(${lineageTintVar(root, this._lastSessions)})`);
        return entry;
    }

    /** Depth-ordered rows within a family so "parent on top" holds even as
     * rows are added/removed across renders — a new row is inserted before
     * the first existing row with a greater depth rather than appended. */
    _ensureRow(entry, depth) {
        let row = entry.rows.get(depth);
        if (!row) {
            row = document.createElement('div');
            row.className = 'topology-row' + (depth === 0 ? ' topology-row--root' : '');
            const deeper = [...entry.rows.entries()]
                .filter(([d]) => d > depth)
                .sort((a, b) => a[0] - b[0])[0];
            if (deeper) entry.familyEl.insertBefore(row, deeper[1]);
            else entry.familyEl.appendChild(row);
            entry.rows.set(depth, row);
        }
        return row;
    }

    _pruneRows(entry, seenRows) {
        for (const [depth, row] of entry.rows) {
            if (!seenRows.has(depth)) {
                row.remove();
                entry.rows.delete(depth);
            }
        }
    }

    _pruneFamilies(seenFamilies) {
        for (const [root, entry] of this._families) {
            if (!seenFamilies.has(root)) {
                entry.familyEl.remove();
                this._families.delete(root);
            }
        }
    }

    _pruneCards(seenCards) {
        for (const [name, entry] of this._cards) {
            if (!seenCards.has(name)) {
                if (entry.expanded) this._collapseCard(name);
                entry.card.remove();
                this._cards.delete(name);
                this._links.delete(name);
            }
        }
    }

    _renderCard(row, session, familyRoot) {
        const name = session.name || '';
        let entry = this._cards.get(name);
        if (!entry) {
            entry = this._buildCard(name);
            this._cards.set(name, entry);
        }
        if (entry.card.parentElement !== row) row.appendChild(entry.card);

        entry.session = session;

        const state = wireStateFor(name, session);
        if (entry.state !== state) {
            if (entry.state) entry.card.classList.remove(`topology-card--${entry.state}`);
            entry.card.classList.add(`topology-card--${state}`);
            entry.state = state;
        }

        if (entry.nameEl.textContent !== name) entry.nameEl.textContent = name;

        const role = (session.roles && session.roles[0]) || 'worker';
        if (entry.roleEl.textContent !== role) {
            entry.roleEl.textContent = role;
            entry.roleEl.classList.toggle('topology-role-chip--orchestrator', role === 'orchestrator');
        }

        const machine = session.machine ? `⌂ ${session.machine}` : '';
        if (entry.machineEl.textContent !== machine) {
            entry.machineEl.textContent = machine;
            entry.machineEl.hidden = !machine;
        }

        const tintVar = lineageTintVar(familyRoot, this._lastSessions);
        if (entry.tintVar !== tintVar) {
            entry.card.style.setProperty('--card-tint', `var(${tintVar})`);
            entry.tintVar = tintVar;
        }
    }

    _buildCard(name) {
        const card = document.createElement('div');
        card.className = 'topology-card';
        card.dataset.session = name;
        card.addEventListener('click', (e) => {
            if (!this._onCardExpand) return;
            // Clicks inside the expanded slot (the mounted mini-terminal, its
            // mic button, etc.) must not bubble into a collapse toggle.
            if (e.target.closest('.topology-card-expand-slot, .topology-card-actions')) return;
            this._toggleExpand(name);
        });

        const top = document.createElement('div');
        top.className = 'topology-card-top';
        const dot = document.createElement('span');
        dot.className = 'topology-status-dot';
        const nameEl = document.createElement('span');
        nameEl.className = 'topology-card-name';
        const roleEl = document.createElement('span');
        roleEl.className = 'topology-role-chip';
        top.append(dot, nameEl, roleEl);

        const sparkEl = document.createElement('div');
        sparkEl.className = 'topology-spark';
        for (let i = 0; i < 5; i++) {
            const bar = document.createElement('i');
            bar.style.animationDelay = `${i * 90}ms`;
            sparkEl.appendChild(bar);
        }
        const machineEl = document.createElement('span');
        machineEl.className = 'topology-card-machine';
        machineEl.hidden = true;

        const meta = document.createElement('div');
        meta.className = 'topology-card-meta';
        meta.append(sparkEl, machineEl);

        card.append(top, meta);

        return {
            card, dot, nameEl, roleEl, machineEl, sparkEl, state: null, tintVar: null, session: null,
            expanded: false, expandSlot: null, expandDispose: null,
        };
    }

    _toggleExpand(name) {
        const entry = this._cards.get(name);
        if (!entry) return;
        if (entry.expanded) this._collapseCard(name);
        else this._expandCard(name);
    }

    _expandCard(name) {
        const entry = this._cards.get(name);
        if (!entry || entry.expanded || !this._onCardExpand) return;
        // Accordion — only one card's mini-terminal (and its live WS) is open
        // at a time, to keep resource use and visual noise bounded.
        if (this._expandedCard && this._expandedCard !== name) {
            this._collapseCard(this._expandedCard);
        }

        const slot = document.createElement('div');
        slot.className = 'topology-card-expand-slot';
        entry.card.appendChild(slot);
        entry.card.classList.add('topology-card--expanded');
        entry.expanded = true;
        entry.expandSlot = slot;
        this._expandedCard = name;

        const dispose = this._onCardExpand(name, entry.session, slot);
        entry.expandDispose = typeof dispose === 'function' ? dispose : null;
        this._scheduleRedraw(); // card grew — links may need repositioning
    }

    _collapseCard(name) {
        const entry = this._cards.get(name);
        if (!entry || !entry.expanded) return;
        entry.expandDispose?.();
        entry.expandDispose = null;
        entry.expandSlot?.remove();
        entry.expandSlot = null;
        entry.card.classList.remove('topology-card--expanded');
        entry.expanded = false;
        if (this._expandedCard === name) this._expandedCard = null;
        this._scheduleRedraw();
    }

    /** Programmatically collapse an expanded card — e.g. the mounted content
     * (a mini-terminal) signals its session ended. No-op if not expanded. */
    collapseCard(name) {
        this._collapseCard(name);
    }

    _scheduleRedraw() {
        if (this._raf !== null) return;
        this._raf = requestAnimationFrame(() => {
            this._raf = null;
            this._redrawLinks();
        });
    }

    _redrawLinks() {
        if (!this._svg) return;
        const rootRect = this._root.getBoundingClientRect();
        const byName = new Map(this._lastSessions.map((s) => [s.name || '', s]));
        const seen = new Set();

        for (const [name, session] of byName) {
            const parentName = session.parent;
            if (!name || !parentName || parentName === name || !byName.has(parentName)) continue;
            const childEntry = this._cards.get(name);
            const parentEntry = this._cards.get(parentName);
            if (!childEntry || !parentEntry) continue;

            const parentRect = parentEntry.card.getBoundingClientRect();
            const childRect = childEntry.card.getBoundingClientRect();
            if (!parentRect.width || !childRect.width) continue; // not laid out (e.g. hidden) yet
            seen.add(name);

            const x1 = parentRect.left + parentRect.width / 2 - rootRect.left;
            const y1 = parentRect.bottom - rootRect.top;
            const x2 = childRect.left + childRect.width / 2 - rootRect.left;
            const y2 = childRect.top - rootRect.top;
            const d = bezierPath(x1, y1, x2, y2);

            let entry = this._links.get(name);
            if (!entry) {
                const path = document.createElementNS(SVG_NS, 'path');
                path.setAttribute('class', 'topology-link');
                this._svg.appendChild(path);
                entry = { path, stateClass: null, tintVar: null };
                this._links.set(name, entry);
            }
            if (entry.path.getAttribute('d') !== d) entry.path.setAttribute('d', d);

            const state = wireStateFor(name, session);
            const stateClass = state === 'idle' ? null : `topology-link--${state}`;
            if (entry.stateClass !== stateClass) {
                if (entry.stateClass) entry.path.classList.remove(entry.stateClass);
                if (stateClass) entry.path.classList.add(stateClass);
                entry.stateClass = stateClass;
            }

            const tintVar = lineageTintVar(name, this._lastSessions);
            if (entry.tintVar !== tintVar) {
                entry.path.style.setProperty('--link-tint', `var(${tintVar})`);
                entry.tintVar = tintVar;
            }
        }

        for (const [name, entry] of this._links) {
            if (!seen.has(name)) {
                entry.path.remove();
                this._links.delete(name);
            }
        }
    }
}
