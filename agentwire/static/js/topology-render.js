/**
 * topology-render.js
 *
 * Mount-agnostic session-family renderer (#761) — the shared engine behind
 * the Session Workspace window (#762), the phantom overlay (#764), and the
 * Session HUD shade (#777, `mode:'shade'`).
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
 * Full-width activity "pulse strip" (#800) — replaces the old 4-5 bar
 * sparkline. A dim baseline plus a heartbeat trace that scrolls left; all of
 * its state (speed, amplitude, colour, the trailing needs-input marker) is
 * driven purely by the card's `topology-card--{idle,flow,awaiting,stuck}`
 * class in CSS, so it stays inside the idempotent render — no per-card
 * animation loop to start or tear down on spawn/kill.
 *
 * The <svg> stretches to card width (`preserveAspectRatio="none"`); the trace
 * spans two identical periods (0..480, viewBox shows one) so the CSS
 * translateX(-240) scroll loops seamlessly. `vector-effect: non-scaling-stroke`
 * keeps the line crisp despite the non-uniform horizontal stretch. The
 * needs-input marker is a DOM span (not an SVG circle) so the stretch can't
 * squash it into an ellipse.
 */
const PULSE_VIEW_W = 240;
const PULSE_BEAT_D =
    'M0 12 H92 L100 9 L106 3 L112 21 L118 9 L126 12 H332 L340 9 L346 3 L352 21 L358 9 L366 12 H480';

function buildPulseStrip() {
    const wrap = document.createElement('div');
    wrap.className = 'topology-pulse';
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'topology-pulse-svg');
    svg.setAttribute('viewBox', `0 0 ${PULSE_VIEW_W} 24`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-hidden', 'true');
    const base = document.createElementNS(SVG_NS, 'line');
    base.setAttribute('class', 'topology-pulse-base');
    base.setAttribute('x1', '0');
    base.setAttribute('y1', '12');
    base.setAttribute('x2', String(PULSE_VIEW_W));
    base.setAttribute('y2', '12');
    const scroll = document.createElementNS(SVG_NS, 'g');
    scroll.setAttribute('class', 'topology-pulse-scroll');
    const trace = document.createElementNS(SVG_NS, 'path');
    trace.setAttribute('class', 'topology-pulse-trace');
    trace.setAttribute('d', PULSE_BEAT_D);
    scroll.appendChild(trace);
    svg.append(base, scroll);
    const marker = document.createElement('span');
    marker.className = 'topology-pulse-marker';
    marker.setAttribute('aria-hidden', 'true');
    wrap.append(svg, marker);
    return wrap;
}

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

/**
 * The label a card should show for a session. Roots keep their full name;
 * children show their distinguishing branch/worktree tail instead of the
 * project-prefixed full name — under the narrow shade the full name truncated
 * every sibling to the same "project…" ellipsis, so children were
 * indistinguishable (#792). Two signals, cwd-immune first:
 *   1. strip the parent's name as a leading prefix — worktree sessions are
 *      named "{project}-{branch}" and the parent ≈ the project — leaving the
 *      branch tail (survives a `cd` inside the session);
 *   2. else the worktree folder basename (path is ~/worktrees/<proj>/<branch>).
 * Falls back to the full bare name when neither yields a distinct tail.
 *
 * @param {{name?: string, parent?: string, path?: string, worktreePath?: string}} session
 * @returns {string}
 */
export function cardDisplayName(session) {
    const raw = (session?.name || '').split('@')[0];
    if (!session?.parent) return raw;
    const parent = String(session.parent).split('@')[0];
    if (parent && raw.length > parent.length && raw.startsWith(parent)) {
        const tail = raw.slice(parent.length).replace(/^[-/_.:\s]+/, '');
        if (tail) return tail;
    }
    const base = (session.path || session.worktreePath || '').replace(/\/+$/, '').split('/').pop();
    if (base && base !== parent && base !== raw) return base;
    return raw;
}

/** Compact git-status badges for a ghost card — same shape and CSS classes
 * (`sidebar-git-badge` + `git-dirty`/`git-clean`/`git-ahead`/`git-behind`/
 * `git-unpushed`/`git-pushed`) as the sidebar's `renderGitBadges`
 * (sidebar/sessions-section.js) so a worktree reads identically whether it's
 * shown as a live session or a ghost. Reimplemented, not imported: the
 * sidebar version isn't just unexported, it's also keyed by session name and
 * closes over that module's private `worktreeGit` map rather than taking a
 * git-status object directly, so reusing it here would mean changing its
 * signature — an edit to sidebar/sessions-section.js, which is out of this
 * feature's edit scope (#801, parallel-worktree file split).
 * @param {{exists?: boolean, dirty?: boolean, staged?: number, unstaged?: number,
 *   untracked?: number, upstream?: string|null, ahead?: number, behind?: number,
 *   pushed?: boolean}|null|undefined} git - `worktree_status()` shape (worktree.py).
 * @returns {string} badge spans HTML, or '' when there's no git status to show.
 */
function gitBadgesHtml(git) {
    if (!git || !git.exists) return '';
    const badges = [];
    if (git.dirty) {
        const n = (git.staged || 0) + (git.unstaged || 0) + (git.untracked || 0);
        badges.push(`<span class="sidebar-git-badge git-dirty" title="${git.staged || 0} staged, ${git.unstaged || 0} unstaged, ${git.untracked || 0} untracked">● dirty${n ? ` ${n}` : ''}</span>`);
    } else {
        badges.push('<span class="sidebar-git-badge git-clean" title="Working tree clean">clean</span>');
    }
    if (!git.upstream) {
        badges.push('<span class="sidebar-git-badge git-unpushed" title="No upstream — branch not pushed">unpushed</span>');
    } else {
        if (git.ahead) badges.push(`<span class="sidebar-git-badge git-ahead" title="${git.ahead} commit(s) ahead of upstream">↑${git.ahead}</span>`);
        if (git.behind) badges.push(`<span class="sidebar-git-badge git-behind" title="${git.behind} commit(s) behind upstream">↓${git.behind}</span>`);
        if (git.pushed && !git.ahead) badges.push('<span class="sidebar-git-badge git-pushed" title="Pushed — upstream up to date">pushed</span>');
    }
    return badges.join('');
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
     * @param {(name: string, session: object, cardEl: HTMLElement) => (() => void)|void} [opts.onSelfMount] -
     *   Fired whenever a card becomes (or, via its returned cleanup, stops being) the "self" session
     *   set via `setSelfSession()`. The self-session equivalent of `onCardExpand` minus the
     *   expand/collapse toggle — used by the Session HUD controller (#778) to mount a header-only PTT
     *   mic onto the dimmed, non-interactive "you-are-here" root card.
     * @param {(name: string, session: object) => Promise<{error?: string, note?: string}>|void} [opts.onGhostCleanup] -
     *   Fired when a ghost card's "Clean up" button is confirmed (session record has `state: 'orphan'`,
     *   #781). May return a promise resolving `{error}` (shown inline, card stays) or `{note}` (shown
     *   inline, informational only — the caller is expected to re-render without this card once the
     *   underlying worktree is actually gone).
     * @param {(name: string, session: object) => Promise<{error?: string}>|void} [opts.onGhostAdopt] -
     *   Fired when a ghost card's "Adopt" button is confirmed. Same contract as `onGhostCleanup`.
     * @param {(name: string, session: object) => void} [opts.onCardOpen] -
     *   Fired from a card's ⋯ menu "Open" item — pop the session into its own full window. Wiring
     *   this (or `onCardKill`) is what surfaces the per-card ⋯ menu button; omit both and cards carry
     *   no menu (the workspace window relies on click-to-expand instead).
     * @param {(name: string, session: object) => void} [opts.onCardKill] -
     *   Fired from a card's ⋯ menu "Kill" item after a two-step in-menu confirm — tear the session down.
     * @param {boolean} [opts.showLinks=true] - Draw the connector SVG layer.
     * @param {'window'|'shade'} [opts.mode='window'] - Styling hook only — 'shade' renders
     *   full-width, left-anchored compact family clusters for the short/narrow Session HUD shade
     *   (#777); 'window' (default) renders solid chrome, centered, for a first-class workspace
     *   window.
     */
    constructor(container, opts = {}) {
        this._container = container;
        this._onCardExpand = opts.onCardExpand || null;
        this._onSelfMount = opts.onSelfMount || null;
        this._onGhostCleanup = opts.onGhostCleanup || null;
        this._onGhostAdopt = opts.onGhostAdopt || null;
        this._onCardOpen = opts.onCardOpen || null;
        this._onCardKill = opts.onCardKill || null;
        /** @type {object|null} card entry whose ⋯ menu is currently open (one at a time) */
        this._openMenuEntry = null;
        this._menuDismiss = null;
        this._showLinks = opts.showLinks !== false;
        this._mode = opts.mode === 'shade' ? 'shade' : 'window';
        this._lastSessions = [];
        /** @type {string|null} name of the currently expanded card, if any (accordion — one at a time) */
        this._expandedCard = null;
        /** @type {string|null} name of the dimmed, non-interactive "you-are-here" root card, if any */
        this._selfSession = null;

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

    /**
     * Set (or clear) the "you-are-here" self session — a dimmed, non-interactive
     * root card (no expand/collapse, no mini-terminal) with its own `onSelfMount`
     * hook (e.g. a header-only mic). Takes effect on the next `render()` call.
     * @param {string|null} name
     */
    setSelfSession(name) {
        this._selfSession = name || null;
    }

    /** Tear down everything TopologyView appended into its container. */
    dispose() {
        if (this._expandedCard) this._collapseCard(this._expandedCard);
        for (const entry of this._cards.values()) {
            if (entry.selfDispose) entry.selfDispose();
            this._closeMenu(entry);
            entry.menuEl?.remove(); // portaled to <body>
            clearTimeout(entry.ghostConfirmTimer);
        }
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
                if (entry.selfDispose) entry.selfDispose();
                this._closeMenu(entry);
                entry.menuEl?.remove(); // portaled to <body>, so card.remove() won't take it
                clearTimeout(entry.ghostConfirmTimer);
                entry.card.remove();
                this._cards.delete(name);
                // Drop the DOM path too: deleting only the map entry orphans its
                // <path> in the SVG (it's gone from _links, so _redrawLinks' stale
                // sweep can't reach it) — a dangling connector after any re-root.
                this._links.get(name)?.path.remove();
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

        const isGhost = session.state === 'orphan';
        if (entry.isGhost !== isGhost) {
            entry.isGhost = isGhost;
            entry.card.classList.toggle('topology-card--ghost', isGhost);
            entry.roleEl.hidden = isGhost;
            entry.pulseEl.hidden = isGhost;
            entry.ghostBadge.hidden = !isGhost;
            entry.ghostInfoEl.hidden = !isGhost;
            entry.ghostGitEl.hidden = !isGhost;
            entry.ghostActions.hidden = !isGhost;
            if (isGhost) { entry.menuBtn.hidden = true; this._closeMenu(entry); }
        }

        const tintVar = lineageTintVar(familyRoot, this._lastSessions);
        if (entry.tintVar !== tintVar) {
            entry.card.style.setProperty('--card-tint', `var(${tintVar})`);
            entry.tintVar = tintVar;
        }

        const label = cardDisplayName(session);
        if (entry.nameEl.textContent !== label) {
            entry.nameEl.textContent = label;
            // Tooltip carries the full session name when the card shows only a tail.
            entry.nameEl.title = label === name ? '' : name;
        }

        if (isGhost) {
            this._renderGhostCard(entry, session);
            return; // ghost cards skip the live-state/role/self styling below
        }

        const state = wireStateFor(name, session);
        if (entry.state !== state) {
            if (entry.state) entry.card.classList.remove(`topology-card--${entry.state}`);
            entry.card.classList.add(`topology-card--${state}`);
            entry.state = state;
        }

        // session.roles (plural) is the arbitrary persona/etiquette list from
        // .agentwire.yml, not the orchestrator/worker axis — do not read it here.
        // session.role (singular) is that axis but is only recorded for sessions
        // created after #747, so long-lived root sessions still have it null;
        // fall back to parentless-ness (this file already treats depth 0 as
        // "root" for row layout above).
        const role = session.role === 'worker' || session.role === 'orchestrator'
            ? session.role
            : (session.parent ? 'worker' : 'orchestrator');
        if (entry.roleEl.textContent !== role) {
            entry.roleEl.textContent = role;
            entry.roleEl.classList.toggle('topology-role-chip--orchestrator', role === 'orchestrator');
        }

        const machine = session.machine ? `⌂ ${session.machine}` : '';
        if (entry.machineEl.textContent !== machine) {
            entry.machineEl.textContent = machine;
            entry.machineEl.hidden = !machine;
        }
        // The meta row now holds only the (usually-absent) machine tag + ghost
        // info, so collapse it when both are hidden — otherwise the empty flex
        // item still draws the card's column-gap as a dead band under the pulse.
        entry.metaEl.hidden = entry.machineEl.hidden && entry.ghostInfoEl.hidden;

        const isSelf = name === this._selfSession;
        if (entry.isSelf !== isSelf) {
            // A card can arrive already-expanded if it was clicked open before
            // becoming the self session (e.g. re-rooting after a card's
            // mini-terminal was opened) — self cards never carry one.
            if (isSelf && entry.expanded) this._collapseCard(name);
            entry.isSelf = isSelf;
            entry.card.classList.toggle('topology-card--self', isSelf);
            if (entry.selfDispose) {
                entry.selfDispose();
                entry.selfDispose = null;
            }
            if (isSelf && this._onSelfMount) {
                const dispose = this._onSelfMount(name, session, entry.card);
                entry.selfDispose = typeof dispose === 'function' ? dispose : null;
            }
        }

        // ⋯ menu: live, non-self cards only, and only when actions are wired
        // (the workspace window omits both callbacks → no menu there). The self
        // card carries a mic instead, and Open/Kill on the session you're already
        // inside is either redundant or a foot-gun.
        const wantMenu = !isSelf && !!(this._onCardOpen || this._onCardKill);
        if (entry.menuBtn.hidden !== !wantMenu) entry.menuBtn.hidden = !wantMenu;
        if (!wantMenu) this._closeMenu(entry);
    }

    _buildCard(name) {
        const card = document.createElement('div');
        card.className = 'topology-card';
        card.dataset.session = name;
        card.addEventListener('click', (e) => {
            // Ghost cards (no live session, #781) are inert outside their two
            // explicit action buttons — there's no session to drill into.
            if (this._cards.get(name)?.isGhost) return;
            // The dimmed "you-are-here" self card is inert — the user is
            // already inside that session, so there's nothing to drill into.
            if (name === this._selfSession) return;
            if (!this._onCardExpand) return;
            // Clicks inside the expanded slot (the mounted mini-terminal, its
            // mic button, etc.), the ⋯ menu, or the ghost actions must not
            // bubble into a collapse toggle.
            if (e.target.closest('.topology-card-expand-slot, .topology-card-actions, .topology-ghost-actions, .topology-card-menu, .topology-card-menu-btn')) return;
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
        const menuBtn = document.createElement('button');
        menuBtn.type = 'button';
        menuBtn.className = 'topology-card-menu-btn';
        menuBtn.title = 'Actions';
        menuBtn.textContent = '⋯';
        menuBtn.hidden = true; // _renderCard shows it for live, non-self cards when actions are wired
        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._toggleMenu(name, this._cards.get(name));
        });
        const ghostBadge = document.createElement('span');
        ghostBadge.className = 'topology-ghost-badge';
        ghostBadge.textContent = 'no session';
        ghostBadge.hidden = true;
        top.append(dot, nameEl, roleEl, menuBtn, ghostBadge);

        const pulseEl = buildPulseStrip();

        const machineEl = document.createElement('span');
        machineEl.className = 'topology-card-machine';
        machineEl.hidden = true;

        const ghostInfoEl = document.createElement('div');
        ghostInfoEl.className = 'topology-ghost-info';
        ghostInfoEl.hidden = true;

        const meta = document.createElement('div');
        meta.className = 'topology-card-meta';
        meta.append(machineEl, ghostInfoEl);

        const ghostGitEl = document.createElement('div');
        ghostGitEl.className = 'topology-ghost-git';
        ghostGitEl.hidden = true;

        const ghostActions = document.createElement('div');
        ghostActions.className = 'topology-ghost-actions';
        ghostActions.hidden = true;
        const cleanupBtn = document.createElement('button');
        cleanupBtn.type = 'button';
        cleanupBtn.className = 'topology-ghost-btn topology-ghost-btn--danger';
        cleanupBtn.textContent = 'Clean up';
        const adoptBtn = document.createElement('button');
        adoptBtn.type = 'button';
        adoptBtn.className = 'topology-ghost-btn topology-ghost-btn--adopt';
        adoptBtn.textContent = 'Adopt';
        const noteEl = document.createElement('div');
        noteEl.className = 'topology-ghost-note';
        noteEl.hidden = true;
        ghostActions.append(cleanupBtn, adoptBtn, noteEl);

        card.append(top, pulseEl, meta, ghostGitEl, ghostActions);

        const entry = {
            card, dot, nameEl, roleEl, machineEl, metaEl: meta, pulseEl, menuBtn, state: null, tintVar: null, session: null,
            expanded: false, expandSlot: null, expandDispose: null,
            isSelf: false, selfDispose: null,
            isGhost: false, ghostBadge, ghostInfoEl, ghostGitEl, ghostActions, cleanupBtn, adoptBtn, noteEl,
            ghostConfirm: null, ghostConfirmTimer: null, ghostBusy: false,
            menuEl: null, menuOpen: false, resetKill: null,
        };

        cleanupBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._handleGhostAction(name, entry, 'cleanup', cleanupBtn);
        });
        adoptBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._handleGhostAction(name, entry, 'adopt', adoptBtn);
        });

        return entry;
    }

    /** Toggle a card's ⋯ action menu — closing any other open one first
     * (accordion: one menu at a time across the whole view). */
    _toggleMenu(name, entry) {
        if (!entry) return;
        if (entry.menuOpen) { this._closeMenu(entry); return; }
        if (this._openMenuEntry && this._openMenuEntry !== entry) this._closeMenu(this._openMenuEntry);
        if (!entry.menuEl) entry.menuEl = this._buildMenu(name, entry);
        entry.menuEl.hidden = false;
        entry.menuOpen = true;
        entry.menuBtn.classList.add('is-open');
        this._openMenuEntry = entry;
        // Position `fixed` from the button's viewport rect (right-aligned under
        // the ⋯, clamped into view). Correct because the menu is portaled to
        // <body> in _buildMenu — see there for why nesting it under the drawer
        // breaks fixed positioning.
        const r = entry.menuBtn.getBoundingClientRect();
        const menu = entry.menuEl;
        menu.style.top = `${Math.round(r.bottom + 4)}px`;
        menu.style.left = 'auto';
        menu.style.right = `${Math.round(Math.max(6, window.innerWidth - r.right))}px`;
        // Dismiss on outside pointerdown or Escape. Deferred bind so the click
        // that opened the menu doesn't immediately close it; capture phase so a
        // click anywhere (including another card) is caught first.
        this._menuDismiss = (e) => {
            if (e.type === 'keydown') { if (e.key === 'Escape') this._closeMenu(entry); return; }
            if (e.target === entry.menuBtn || entry.menuEl.contains(e.target)) return;
            this._closeMenu(entry);
        };
        setTimeout(() => {
            if (!entry.menuOpen) return;
            document.addEventListener('pointerdown', this._menuDismiss, true);
            document.addEventListener('keydown', this._menuDismiss, true);
        }, 0);
    }

    _closeMenu(entry) {
        if (!entry || !entry.menuOpen) return;
        entry.menuOpen = false;
        entry.menuBtn?.classList.remove('is-open');
        if (entry.menuEl) entry.menuEl.hidden = true;
        entry.resetKill?.(); // disarm a half-confirmed Kill
        if (this._menuDismiss) {
            document.removeEventListener('pointerdown', this._menuDismiss, true);
            document.removeEventListener('keydown', this._menuDismiss, true);
            this._menuDismiss = null;
        }
        if (this._openMenuEntry === entry) this._openMenuEntry = null;
    }

    /** Build the ⋯ popover: an "Open" item (pop to full window) and a "Kill"
     * item guarded by an in-menu two-step confirm (mirrors the sidebar close
     * button's "click, then click again to confirm" pattern). Items appear only
     * for the callbacks the caller wired. */
    _buildMenu(name, entry) {
        const menu = document.createElement('div');
        menu.className = 'topology-card-menu';
        menu.hidden = true;
        menu.addEventListener('click', (e) => e.stopPropagation());

        if (this._onCardOpen) {
            const openBtn = document.createElement('button');
            openBtn.type = 'button';
            openBtn.className = 'topology-card-menu-item';
            openBtn.innerHTML = '<span class="topology-card-menu-icon">⤢</span>Open window';
            openBtn.addEventListener('click', () => {
                this._closeMenu(entry);
                this._onCardOpen(name, entry.session);
            });
            menu.appendChild(openBtn);
        }

        if (this._onCardKill) {
            const KILL_HTML = '<span class="topology-card-menu-icon">✕</span>Kill session';
            const killBtn = document.createElement('button');
            killBtn.type = 'button';
            killBtn.className = 'topology-card-menu-item topology-card-menu-item--danger';
            killBtn.innerHTML = KILL_HTML;
            let armed = false;
            let timer = null;
            entry.resetKill = () => {
                armed = false;
                if (timer) { clearTimeout(timer); timer = null; }
                killBtn.innerHTML = KILL_HTML;
                killBtn.classList.remove('is-armed');
            };
            killBtn.addEventListener('click', () => {
                if (!armed) {
                    armed = true;
                    killBtn.textContent = 'Confirm kill';
                    killBtn.classList.add('is-armed');
                    timer = setTimeout(() => entry.resetKill?.(), 3000);
                    return;
                }
                this._closeMenu(entry);
                this._onCardKill(name, entry.session);
            });
            menu.appendChild(killBtn);
        }

        // Portal to <body>, NOT the card: the HUD drawer's backdrop-filter +
        // transform each make it the containing block for position:fixed
        // descendants, so a menu nested under it would be positioned/clipped
        // relative to the drawer (rendering "inside the fold") instead of the
        // viewport. On body there's no such ancestor, so the fixed coords set
        // from the button's viewport rect land correctly and nothing clips.
        document.body.appendChild(menu);
        return menu;
    }

    /** Ghost cards (session.state === 'orphan', #781) skip the live-status
     * dot/spark/role logic and just show what's on disk: branch + worktree
     * path, read-only git-status badges (#801), and the two action buttons
     * built in `_buildCard`. */
    _renderGhostCard(entry, session) {
        const info = [
            session.branch ? `⎇ ${session.branch}` : null,
            session.worktreePath || null,
        ].filter(Boolean).join('  ·  ');
        if (entry.ghostInfoEl.textContent !== info) entry.ghostInfoEl.textContent = info;
        entry.ghostInfoEl.hidden = !info;
        entry.machineEl.hidden = true;
        entry.metaEl.hidden = entry.machineEl.hidden && entry.ghostInfoEl.hidden;

        const gitHtml = gitBadgesHtml(session.git);
        if (entry.ghostGitEl.innerHTML !== gitHtml) entry.ghostGitEl.innerHTML = gitHtml;
        entry.ghostGitEl.hidden = !gitHtml;
    }

    /** Two-step confirm (matches the sidebar's close-button "sure?" pattern) —
     * first click on either button arms it and disarms the other; a second
     * click on the SAME button within the window fires the action. Busy/error
     * state is shown inline on the card; the caller (session-hud-controller.js)
     * is responsible for re-rendering without this card once the underlying
     * worktree is actually gone. */
    _handleGhostAction(name, entry, kind, btn) {
        if (entry.ghostBusy) return;

        if (entry.ghostConfirm !== kind) {
            entry.ghostConfirm = kind;
            clearTimeout(entry.ghostConfirmTimer);
            entry.cleanupBtn.textContent = kind === 'cleanup' ? 'sure?' : 'Clean up';
            entry.adoptBtn.textContent = kind === 'adopt' ? 'sure?' : 'Adopt';
            entry.ghostConfirmTimer = setTimeout(() => {
                entry.ghostConfirm = null;
                entry.cleanupBtn.textContent = 'Clean up';
                entry.adoptBtn.textContent = 'Adopt';
            }, 3000);
            return;
        }

        clearTimeout(entry.ghostConfirmTimer);
        entry.ghostConfirm = null;
        const handler = kind === 'cleanup' ? this._onGhostCleanup : this._onGhostAdopt;
        if (!handler) return;

        entry.ghostBusy = true;
        entry.cleanupBtn.disabled = true;
        entry.adoptBtn.disabled = true;
        btn.textContent = kind === 'cleanup' ? 'Removing…' : 'Adopting…';
        entry.noteEl.hidden = true;

        Promise.resolve(handler(name, entry.session))
            .then((result) => {
                const msg = result && (result.error || result.note);
                if (msg) {
                    entry.noteEl.textContent = msg;
                    entry.noteEl.hidden = false;
                }
            })
            .catch((err) => {
                entry.noteEl.textContent = err?.message || 'Action failed';
                entry.noteEl.hidden = false;
            })
            .finally(() => {
                entry.ghostBusy = false;
                entry.cleanupBtn.disabled = false;
                entry.adoptBtn.disabled = false;
                entry.cleanupBtn.textContent = 'Clean up';
                entry.adoptBtn.textContent = 'Adopt';
            });
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

            let stateClass = null;
            if (session.state === 'orphan') {
                stateClass = 'topology-link--ghost';
            } else {
                const state = wireStateFor(name, session);
                stateClass = state === 'idle' ? null : `topology-link--${state}`;
            }
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
