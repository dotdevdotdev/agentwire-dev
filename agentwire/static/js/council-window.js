/**
 * Council workspace window — a WinBox window that makes a live sitting legible
 * AND operable: a left rail to seat/dismiss the council, read the question,
 * switch rounds, and re-prompt; a wide multi-column tile grid filling the rest.
 * Each lens (soul) is a tile showing only its *filed verdict*; tiles swap once,
 * cleanly, when a reply lands (the `council_update` WS delta) — no token
 * streaming, no flicker.
 *
 * The tile treatment keys off a single status class
 * `council-tile--{pending,acked,answered,passed,stalled}`, styled in
 * desktop.css; state colors flow through the scoped `--council-{state}` alias
 * vars. This module owns the *real* data path and renders that markup.
 *
 * Window behaviour matches a session window: it registers with the desktop
 * manager (so it opens maximized, joins the open-sessions list, and is
 * tabbable). The registration wrapper lives in desktop.js (`openCouncilWindow`);
 * this module exports the `CouncilWindow` class it instantiates.
 *
 * Data flow:
 *   - first paint / round switch / reconnect → GET /api/council/live (snapshot)
 *   - rail seating/liveness → GET /api/council/status (per-soul session liveness)
 *   - per-reply deltas → desktop 'council_update' { sitting, prompt_id, tile }
 *     (or { reset:true } on a new round, { seating:true } on seat,
 *     { stopped:true } on dismiss); the filed tile gets the swap animation.
 *   - seat/dismiss/ask → POST /api/council/{start,stop,ask}
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';

export const COUNCIL_WINDOW_ID = 'council-board';

// View state — one board at a time (re-opening focuses the existing window).
const state = {
    sitting: null,        // sitting <name> currently shown
    promptId: null,       // prompt id currently shown
    latestPromptId: null, // newest round for the sitting (live vs history)
    promptIds: [],        // every round id, ascending (selector)
    promptText: '',
    roster: [],           // fixed soul order — never reordered under the user
    createdAt: '',        // round start (drives the pending/stalled elapsed meta)
    tiles: new Map(),     // soul -> { soul, status, kind, verdict, filed_at }
    sittings: [],         // every live sitting (for the sitting picker)
    roundTexts: {},       // prompt_id -> question text, cached as rounds are visited
    liveness: {},         // soul -> bool (lens session alive — from /status)
    seated: false,        // is a sitting present at all
    archived: false,      // viewing a dismissed thread (no live sessions)
    running: true,        // a live sitting backs the view
    busy: false,          // a seat/ask/dismiss POST is in flight
};

let container = null;     // the active window's mount (set by CouncilWindow)
let timerInterval = null;

// chip copy per status (reference CHIP map)
const CHIP = {
    pending: 'DELIBERATING',
    acked: 'RESEARCHING',
    answered: 'TAKE',
    passed: 'PASSED',
    stalled: 'STALLED',
};

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

/** Whether a tile counts toward "N of M in" — terminal states only (#403). */
function isFinal(status) {
    return status === 'answered' || status === 'passed';
}

/** Body copy per status — real verdict where we have one, reference copy else. */
function bodyFor(tile) {
    const verdict = String(tile.verdict || '').trim();
    switch (tile.status) {
        case 'answered': return verdict;
        case 'acked': return verdict || 'Filed a holding note — a fuller answer is on the way.';
        case 'passed': return verdict || 'Nothing to add this round; the others have it covered.';
        case 'stalled': return 'No response — the soul stalled before filing.';
        default: return '';
    }
}

/** The mono meta line (timer / "will follow up" / "no response"). */
function metaFor(tile) {
    switch (tile.status) {
        case 'pending': return { since: state.createdAt, suffix: '' };
        case 'stalled': return { since: state.createdAt, suffix: ' · no response' };
        case 'acked': return { text: 'will follow up' };
        default: return null;
    }
}

function elapsedSince(iso) {
    if (!iso) return '';
    const start = Date.parse(iso);
    if (Number.isNaN(start)) return '';
    const secs = Math.max(0, Math.floor((Date.now() - start) / 1000));
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    return `${hrs}h ${mins % 60}m`;
}

function pad2(n) {
    return String(n).padStart(2, '0');
}

// ── Webfonts (progressive enhancement — fallback stack holds if blocked) ──────

let fontsInjected = false;
function ensureFonts() {
    if (fontsInjected || document.getElementById('council-webfonts')) {
        fontsInjected = true;
        return;
    }
    fontsInjected = true;
    const pre1 = document.createElement('link');
    pre1.rel = 'preconnect'; pre1.href = 'https://fonts.googleapis.com';
    const pre2 = document.createElement('link');
    pre2.rel = 'preconnect'; pre2.href = 'https://fonts.gstatic.com'; pre2.crossOrigin = 'anonymous';
    const css = document.createElement('link');
    css.id = 'council-webfonts';
    css.rel = 'stylesheet';
    css.href = 'https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=Space+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap';
    document.head.append(pre1, pre2, css);
}

// ── Snapshot fetch ──────────────────────────────────────────────────────────

async function loadSnapshot(sitting, promptId) {
    const params = new URLSearchParams();
    if (sitting) params.set('sitting', sitting);
    if (promptId != null) params.set('prompt_id', String(promptId));
    const qs = params.toString();
    const res = await apiFetch(`/api/council/live${qs ? `?${qs}` : ''}`);
    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        state.sittings = body.sittings || [];
        state.sitting = null;
        state.seated = state.sittings.length > 0;
        // No sittings at all → the seatable unseated workspace. Ambiguous (409)
        // or a transient error → a picker / message.
        if (!state.sittings.length) {
            render();
        } else {
            renderPicker();
        }
        return;
    }
    applySnapshot(await res.json());
    loadStatus(state.sitting);  // fire-and-forget — fills the liveness dots
}

function applySnapshot(snap) {
    state.sitting = snap.sitting;
    state.seated = true;
    state.archived = !!snap.archived;       // dismissed thread, no live sessions
    state.running = snap.running !== false;  // live sitting present
    state.promptId = snap.prompt_id;
    state.promptIds = snap.prompt_ids || [];
    state.latestPromptId = state.promptIds.length
        ? state.promptIds[state.promptIds.length - 1]
        : snap.prompt_id;
    state.promptText = snap.prompt_text || '';
    state.roster = snap.roster || [];
    state.createdAt = snap.created_at || '';
    state.sittings = snap.sittings || [];
    if (snap.prompt_id != null) state.roundTexts[snap.prompt_id] = snap.prompt_text || '';
    state.tiles = new Map();
    for (const tile of snap.tiles || []) state.tiles.set(tile.soul, tile);
    render();
}

/** Per-soul session liveness (the rail's seating dots). Best-effort. */
async function loadStatus(sitting) {
    if (!sitting) return;
    try {
        const res = await apiFetch(`/api/council/status?sitting=${encodeURIComponent(sitting)}`);
        if (!res.ok) return;
        const data = await res.json();
        if (sitting !== state.sitting) return;  // raced past a round/sitting switch
        const map = {};
        for (const s of data.souls || []) map[s.soul] = !!s.alive;
        state.liveness = map;
        refreshSeating();
    } catch { /* ignore */ }
}

// ── Delta handling (the show) ─────────────────────────────────────────────────

function onCouncilUpdate(msg) {
    if (!container) return;

    // Seat/dismiss events can arrive when we hold no sitting yet (unseated
    // workspace) — handle them before the per-sitting filter.
    if (msg.seating) {
        // A council was just seated; follow it.
        loadSnapshot(msg.sitting || null, null);
        return;
    }
    if (msg.stopped) {
        if (!state.sitting || msg.sitting === state.sitting) {
            loadSnapshot(null, null);  // re-resolve (other sittings may remain)
        }
        return;
    }

    if (msg.sitting !== state.sitting) return;
    const viewingLatest = state.promptId === state.latestPromptId;

    if (msg.reset) {
        // New round — follow it if live, else just refresh the round list.
        loadSnapshot(state.sitting, viewingLatest || state.promptId == null ? null : state.promptId);
        return;
    }
    if (!msg.tile) return;
    if (msg.prompt_id !== state.promptId) return;  // delta for a different round

    const prev = state.tiles.get(msg.tile.soul);
    state.tiles.set(msg.tile.soul, msg.tile);
    swapTile(msg.tile, prev);
}

/** Replace a single tile in place with the swap animation — no full re-render. */
function swapTile(tile, prev) {
    const el = container?.querySelector(`.council-tile[data-soul="${cssEscape(tile.soul)}"]`);
    if (!el) { render(); return; }
    el.outerHTML = tileHtml(tile);
    const fresh = container.querySelector(`.council-tile[data-soul="${cssEscape(tile.soul)}"]`);
    if (fresh) {
        bindTile(fresh);
        if (!prev || prev.status !== tile.status) {
            fresh.classList.add('council-tile--flip');
            fresh.addEventListener('animationend', () => fresh.classList.remove('council-tile--flip'), { once: true });
        }
    }
    updateCounter();
    if (allFinal()) flourish();
}

function allFinal() {
    if (!state.roster.length) return false;
    return state.roster.every((s) => isFinal(state.tiles.get(s)?.status));
}

function cssEscape(s) {
    return String(s).replace(/["\\]/g, '\\$&');
}

// ── Rendering ──────────────────────────────────────────────────────────────────

function tileHtml(tileOrSoul) {
    const tile = typeof tileOrSoul === 'string'
        ? (state.tiles.get(tileOrSoul) || { soul: tileOrSoul, status: 'pending' })
        : tileOrSoul;
    const status = tile.status || 'pending';
    const meta = metaFor(tile);
    let metaHtml = '';
    if (meta) {
        if (meta.text) {
            metaHtml = `<span class="council-meta">${esc(meta.text)}</span>`;
        } else {
            metaHtml = `<span class="council-meta" data-since="${esc(meta.since)}" data-suffix="${esc(meta.suffix)}">· ${esc(elapsedSince(meta.since))}${esc(meta.suffix)}</span>`;
        }
    }
    const body = bodyFor(tile);
    const down = state.liveness[tile.soul] === false ? ' council-tile--down' : '';
    return `
        <div class="council-tile council-tile--${esc(status)}${down}" data-soul="${esc(tile.soul)}">
            <div class="council-tile-top">
                <div class="council-tile-headline">
                    <span class="council-tile-led" title="session liveness"></span>
                    <span class="council-tile-name">${esc(tile.soul)}</span>
                    <span class="council-chip"><span class="council-chip-dot"></span>${esc(CHIP[status] || status)}</span>
                    ${metaHtml}
                </div>
                <div class="council-tile-expand">Read full verdict <span>→</span></div>
            </div>
            <div class="council-tile-body">${esc(body)}</div>
        </div>`;
}

function counterText() {
    const final = state.roster.filter((s) => isFinal(state.tiles.get(s)?.status)).length;
    return `${final} of ${state.roster.length} in`;
}

/** Seat status + control for the header: counter + Dismiss when seated,
 *  Seat button when not. Per-soul liveness now rides on the tiles. */
function seatControlHtml() {
    if (!state.seated || !state.sitting) {
        return `
            <button class="council-btn council-btn--primary council-btn--compact" data-action="seat" ${state.busy ? 'disabled' : ''}>
                ${state.busy ? 'Seating…' : 'Seat the council'}
            </button>`;
    }
    if (state.archived) {
        // Dismissed thread: read-only, no live sessions to dismiss. Re-asking
        // (from the ask row) re-seats the same roster under the same name.
        return `
            <span class="council-seat-summary council-seat-summary--archived">
                <span class="council-counter">${esc(counterText())}</span>
                <span class="council-archived-tag">ARCHIVED</span>
            </span>`;
    }
    return `
        <span class="council-seat-summary"><span class="council-seat-led"></span><span class="council-counter">${esc(counterText())}</span></span>
        <button class="council-btn council-btn--ghost" data-action="dismiss" ${state.busy ? 'disabled' : ''}>Dismiss</button>`;
}

/** Re-render only the seat control (liveness/counter arrives after the snapshot). */
function refreshSeating() {
    const slot = container?.querySelector('[data-slot="seating"]');
    if (slot) {
        slot.innerHTML = seatControlHtml();
        bindSeating();
    }
    applyTileLiveness();
}

/** Toggle the per-tile down state from the latest /status liveness map. */
function applyTileLiveness() {
    container?.querySelectorAll('.council-tile').forEach((el) => {
        const soul = el.dataset.soul;
        el.classList.toggle('council-tile--down', !!soul && state.liveness[soul] === false);
    });
}

function questionHtml() {
    if (!state.promptText) {
        return `<div class="council-q council-q--empty">No prompt asked yet.</div>`;
    }
    return `<div class="council-q"><span class="council-quote">“</span>${esc(state.promptText)}<span class="council-quote">”</span></div>`;
}

/** The round breadcrumb at the top-right of the header — a compact dropdown
 *  that swaps which round's verdicts the board is showing. */
function roundsCrumbHtml() {
    if (!state.seated || !state.promptIds.length) return '';
    const cur = state.promptId;
    const curLatest = cur === state.latestPromptId;
    const curTag = `ROUND ${pad2(cur)}${curLatest ? ' · LATEST' : ''}`;
    const items = state.promptIds.slice().reverse().map((id) => {
        const isLatest = id === state.latestPromptId;
        const tag = `ROUND ${pad2(id)}${isLatest ? ' · LATEST' : ''}`;
        const txt = state.roundTexts[id] || (isLatest ? 'latest round' : `prompt #${id}`);
        return `
            <button class="council-round-item ${id === cur ? 'council-round-item--active' : ''}" data-round="${id}">
                <span class="council-round-tag">${esc(tag)}</span>
                <span class="council-round-txt">${esc(txt)}</span>
            </button>`;
    }).join('');
    return `
        <div class="council-crumb" data-slot="rounds-crumb">
            <button class="council-crumb-btn" data-action="rounds-toggle">
                <span class="council-crumb-label">${esc(curTag)}</span>
                <span class="council-crumb-chev">▾</span>
            </button>
            <div class="council-crumb-menu">${items}</div>
        </div>`;
}

/** The header ASK row — re-prompt the seated sitting; compact input + button. */
function askHtml() {
    if (!state.seated || !state.sitting) return '';
    const placeholder = state.archived
        ? 'Re-ask this thread — re-seats the same lenses…'
        : 'Ask the council, or add context…';
    const label = state.busy
        ? (state.archived ? 'Re-seating…' : 'Asking…')
        : (state.archived ? 'Re-seat &amp; ask →' : 'Ask the council →');
    return `
        <div class="council-ask">
            <div class="council-ask-row">
                <textarea class="council-ask-input" rows="1" placeholder="${esc(placeholder)}" ${state.busy ? 'disabled' : ''}></textarea>
                <button class="council-btn council-btn--primary council-btn--compact" data-action="ask" ${state.busy ? 'disabled' : ''}>
                    ${label}
                </button>
            </div>
            <div class="council-ask-err" data-slot="ask-err"></div>
        </div>`;
}

/** The main-area tile grid (seated + a prompt asked) or a calm empty state. */
function mainHtml() {
    if (!state.seated || !state.sitting) {
        return `
            <div class="council-main-empty">
                <div class="council-main-empty-title">The chamber is empty</div>
                <div class="council-main-empty-sub">Seat the council from the header to convene the souls.</div>
            </div>`;
    }
    if (!state.promptId || !state.roster.length) {
        return `
            <div class="council-main-empty">
                <div class="council-main-empty-title">The council is seated</div>
                <div class="council-main-empty-sub">Ask the first question from the header and the takes will fill in here.</div>
            </div>`;
    }
    const trio = state.roster.length <= 3 ? ' council-grid--trio' : '';
    return `
        <div class="council-panel">
            <div class="council-panel-glow"></div>
            <div class="council-grid${trio}">
                ${state.roster.map((s) => tileHtml(s)).join('')}
            </div>
        </div>`;
}

function render() {
    if (!container) return;
    const sittingLabel = state.sitting || 'No council seated';
    const live = state.promptId === state.latestPromptId;
    const seated = state.seated && state.sitting;
    container.innerHTML = `
        <div class="council-glow"></div>
        <div class="council-vignette"></div>
        <header class="council-header">
            <div class="council-header-main">
                <div class="council-topbar-left">
                    <div class="council-topbar-title">
                        <div class="council-brand"><div class="council-led"></div><div class="council-eyebrow">AGENTWIRE · COUNCIL</div></div>
                        <div class="council-sitting">${esc(sittingLabel)}</div>
                    </div>
                    ${sittingSelectorInline()}
                </div>
                <div class="council-header-controls">
                    <span class="council-seat-control" data-slot="seating">${seatControlHtml()}</span>
                    ${roundsCrumbHtml()}
                </div>
            </div>
            ${seated ? `
            <div class="council-header-q">
                <span class="council-q-label">${live ? 'QUESTION' : 'EARLIER ROUND'}</span>
                ${questionHtml()}
            </div>
            ${askHtml()}
            ` : ''}
        </header>
        <main class="council-main">${mainHtml()}</main>`;
    bindBoard();
    startTimer();
    if (state.seated && allFinal()) markCompleteStatic();
}

/** A native <select> to switch sittings when more than one is live. */
function sittingSelectorInline() {
    if (state.sittings.length <= 1) return '';
    const opts = state.sittings
        .map((n) => `<option value="${esc(n)}" ${n === state.sitting ? 'selected' : ''}>${esc(n)}</option>`)
        .join('');
    return `<select class="council-round-select" data-action="sitting" style="margin-top:10px">${opts}</select>`;
}

/** Ambiguous (multiple sittings, none picked) or transient — show a picker. */
function renderPicker() {
    if (!container) return;
    stopTimer();
    const picker = state.sittings.length
        ? `<select class="council-round-select" data-action="sitting">
               <option value="" disabled selected>Choose a sitting…</option>
               ${state.sittings.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}
           </select>`
        : '';
    container.innerHTML = `
        <div class="council-glow"></div>
        <div class="council-shell council-shell--empty">
            <div class="council-empty">Multiple council sittings are live — pick one.</div>
            ${picker}
        </div>`;
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => {
        if (e.target.value) loadSnapshot(e.target.value, null);
    });
}

function renderLoading(message) {
    if (!container) return;
    stopTimer();
    container.innerHTML = `
        <div class="council-glow"></div>
        <div class="council-shell council-shell--empty">
            <div class="council-empty">${esc(message)}</div>
        </div>`;
}

function updateCounter() {
    const el = container?.querySelector('.council-counter');
    if (el) el.textContent = counterText();
}

/** Resting board that's already complete on open — green counter, no pop. */
function markCompleteStatic() {
    container?.querySelector('.council-counter')?.classList.add('council-counter--complete');
}

/** The "sitting complete" flourish — green counter, pop, panel glow. */
function flourish() {
    const counter = container?.querySelector('.council-counter');
    if (counter) {
        counter.classList.add('council-counter--complete');
        counter.classList.remove('council-counter--pop');
        void counter.offsetWidth;
        counter.classList.add('council-counter--pop');
    }
    const glow = container?.querySelector('.council-panel-glow');
    if (glow) {
        glow.classList.remove('council-panel-glow--show');
        void glow.offsetWidth;
        glow.classList.add('council-panel-glow--show');
    }
}

let crumbCloserBound = false;
/** Close the round breadcrumb dropdown on any outside click (bound once). */
function bindCrumbCloser() {
    if (crumbCloserBound) return;
    crumbCloserBound = true;
    document.addEventListener('click', () => {
        container?.querySelector('[data-slot="rounds-crumb"]')?.classList.remove('council-crumb--open');
    });
}

function bindBoard() {
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => {
        if (e.target.value) loadSnapshot(e.target.value, null);
    });
    // Round breadcrumb — toggle the dropdown; items swap the viewed round.
    const crumb = container.querySelector('[data-slot="rounds-crumb"]');
    crumb?.querySelector('[data-action="rounds-toggle"]')?.addEventListener('click', (e) => {
        e.stopPropagation();
        crumb.classList.toggle('council-crumb--open');
    });
    container.querySelectorAll('.council-round-item').forEach((btn) => {
        btn.addEventListener('click', () => loadSnapshot(state.sitting, Number(btn.dataset.round)));
    });
    bindCrumbCloser();
    bindSeating();
    bindAsk();
    // Sitting name → focus the orchestrator session.
    const sit = container.querySelector('.council-sitting');
    if (sit && state.sitting) {
        sit.classList.add('council-sitting--link');
        sit.title = 'Open the orchestrator session';
        sit.addEventListener('click', openOrchestratorSession);
    }
    container.querySelectorAll('.council-tile').forEach(bindTile);
}

function bindSeating() {
    container.querySelector('[data-action="seat"]')?.addEventListener('click', seatCouncil);
    container.querySelector('[data-action="dismiss"]')?.addEventListener('click', dismissCouncil);
}

function bindAsk() {
    const btn = container.querySelector('[data-action="ask"]');
    const input = container.querySelector('.council-ask-input');
    if (!btn || !input) return;
    btn.addEventListener('click', () => askCouncil(input));
    // Cmd/Ctrl+Enter sends; plain Enter keeps newlines (it's a textarea).
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            askCouncil(input);
        }
        e.stopPropagation();  // don't leak keystrokes to global shortcuts
    });
}

/**
 * Souls and the orchestrator are real sessions — jump to them from the board.
 * Opening a session window flips the desktop to single-window mode (it minimizes
 * the council window), exactly like clicking any session elsewhere.
 */
async function openSoulSession(soul) {
    if (!state.sitting) return;
    const { openSessionTerminal } = await import('./desktop.js');
    openSessionTerminal(`council-${state.sitting}-${soul}`, 'terminal');
}

async function openOrchestratorSession() {
    if (!state.sitting) return;
    const { openSessionTerminal } = await import('./desktop.js');
    openSessionTerminal(`agentwire-council-${state.sitting}`, 'terminal');
}

function bindTile(el) {
    const soul = el.dataset.soul;
    // Soul name → open/focus that soul's session window (any status).
    const nameEl = el.querySelector('.council-tile-name');
    if (nameEl) {
        nameEl.classList.add('council-tile-name--link');
        nameEl.title = `Open ${soul}'s session`;
        nameEl.addEventListener('click', (e) => { e.stopPropagation(); openSoulSession(soul); });
    }
    const tile = state.tiles.get(soul);
    if (!tile || tile.status !== 'answered') return;  // only the hero state opens
    const open = () => openReader(soul);
    el.addEventListener('click', open);
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
}

// ── Seat / dismiss / ask (the operable rail) ──────────────────────────────────

async function seatCouncil() {
    if (state.busy) return;
    state.busy = true;
    refreshSeating();
    try {
        const res = await apiFetch('/api/council/start', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            // Surface the CLI's reason in the seating slot, then recover.
            state.busy = false;
            refreshSeating();
            setSeatError(data.error || 'Could not seat the council.');
            return;
        }
        const seated = data.council;
        state.busy = false;
        // The WS seating delta will also fire; loading now makes it feel instant.
        if (seated) loadSnapshot(seated, null);
        else loadSnapshot(null, null);
    } catch (e) {
        state.busy = false;
        refreshSeating();
        setSeatError('Could not reach the portal.');
    }
}

function setSeatError(message) {
    const slot = container?.querySelector('[data-slot="seating"]');
    if (!slot) return;
    let err = slot.querySelector('.council-seat-err');
    if (!err) {
        err = document.createElement('div');
        err.className = 'council-seat-err';
        slot.appendChild(err);
    }
    err.textContent = message;
}

async function dismissCouncil() {
    if (state.busy || !state.sitting) return;
    state.busy = true;
    refreshSeating();
    const sitting = state.sitting;
    try {
        await apiFetch('/api/council/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sitting }),
        });
    } catch { /* the WS stopped delta / re-resolve handles the rest */ }
    state.busy = false;
    loadSnapshot(null, null);
}

async function askCouncil(input) {
    if (state.busy || !state.sitting) return;
    const prompt = input.value.trim();
    if (!prompt) { input.focus(); return; }
    state.busy = true;
    const btn = container?.querySelector('[data-action="ask"]');
    const reseating = state.archived || !state.running;
    if (btn) { btn.disabled = true; btn.textContent = reseating ? 'Re-seating…' : 'Asking…'; }
    input.disabled = true;
    setAskError('');
    try {
        // Archived thread → re-seat the same roster under the same name first,
        // so the fan-out has live lens sessions to land in.
        if (reseating) {
            const startBody = { sitting: state.sitting };
            if (state.roster.length) startBody.roster = state.roster.join(',');
            const sres = await apiFetch('/api/council/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(startBody),
            });
            const sdata = await sres.json().catch(() => ({}));
            if (!sres.ok) {
                state.busy = false;
                if (btn) { btn.disabled = false; }
                input.disabled = false;
                setAskError(sdata.error || 'Could not re-seat the council.');
                return;
            }
            state.archived = false;
            state.running = true;
            if (btn) btn.textContent = 'Asking…';
        }
        const res = await apiFetch('/api/council/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sitting: state.sitting, prompt }),
        });
        const data = await res.json().catch(() => ({}));
        state.busy = false;
        if (!res.ok) {
            if (btn) { btn.disabled = false; btn.textContent = 'Ask the council →'; }
            input.disabled = false;
            setAskError(data.error || 'Could not send the prompt.');
            return;
        }
        input.value = '';
        // Switch to the freshly-created round; the WS reset would do this too.
        loadSnapshot(state.sitting, null);
    } catch (e) {
        state.busy = false;
        if (btn) { btn.disabled = false; btn.textContent = 'Ask the council →'; }
        input.disabled = false;
        setAskError('Could not reach the portal.');
    }
}

function setAskError(message) {
    const slot = container?.querySelector('[data-slot="ask-err"]');
    if (slot) slot.textContent = message;
}

// ── Reader overlay (click-to-expand full verdict) ─────────────────────────────

function openReader(soul) {
    const tile = state.tiles.get(soul);
    if (!tile || !tile.verdict) return;
    closeReader();
    const backdrop = document.createElement('div');
    backdrop.className = 'council-backdrop council-backdrop--open';
    backdrop.dataset.councilReader = '1';
    backdrop.innerHTML = `
        <div class="council-reader" role="dialog" aria-label="${esc(soul)} verdict">
            <button class="council-reader-close" aria-label="Close">✕</button>
            <div class="council-reader-head"><div class="council-reader-name">${esc(soul)}</div></div>
            <div class="council-reader-chip">${esc(CHIP[tile.status] || tile.status)} · FILED</div>
            <div class="council-reader-body">${esc(tile.verdict)}</div>
        </div>`;
    const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    backdrop.querySelector('.council-reader-close').addEventListener('click', close);
    backdrop.querySelector('.council-reader').addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('keydown', onKey);
    container.appendChild(backdrop);
}

function closeReader() {
    container?.querySelector('[data-council-reader]')?.remove();
}

// ── Pending/stalled elapsed timer ──────────────────────────────────────────────

function startTimer() {
    stopTimer();
    timerInterval = setInterval(() => {
        if (!container) return;
        container.querySelectorAll('.council-meta[data-since]').forEach((el) => {
            el.textContent = `· ${elapsedSince(el.dataset.since)}${el.dataset.suffix || ''}`;
        });
    }, 1000);
}

function stopTimer() {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}

// ── Window wrapper ───────────────────────────────────────────────────────────

let listenerBound = false;

/**
 * CouncilWindow — mirrors the SessionWindow/ArtifactWindow surface so desktop.js
 * can register it (open maximized, taskbar tab, single-window mode). The board
 * is a module-level singleton (only one sitting is shown at a time), so this is
 * a thin lifecycle wrapper that owns the WinBox and points `container` at it.
 */
export class CouncilWindow {
    constructor(options = {}) {
        this.id = COUNCIL_WINDOW_ID;
        this.title = 'Council';
        this.root = options.root || document.body;
        this.sitting = options.sitting || null;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;
        this.winbox = null;
        this.isOpen = false;
    }

    open() {
        if (this.isOpen) { this.focus(); return; }
        ensureFonts();
        container = document.createElement('div');
        container.className = 'council-window-mount';

        this.winbox = new WinBox({
            title: this.title,
            icon: '<span style="font-size:14px">🏛️</span>',
            mount: container,
            root: this.root,
            width: '100%',
            height: '100%',
            minwidth: 480,
            minheight: 380,
            class: ['council-window', 'no-full', 'no-resize', 'no-move'],
            onclose: () => {
                this.winbox = null;
                this.close();
                return false;
            },
            onfocus: () => { if (this.onFocusCallback) this.onFocusCallback(this); },
            onminimize: () => { desktop.emit('window_minimized', { id: this.id }); },
            onrestore: () => {
                desktop.emit('window_restored', { id: this.id });
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
        });

        // Always open maximized; registering also flips to single-window mode.
        this.winbox.maximize();
        desktop.registerWindow(this.id, this.winbox);

        if (!listenerBound) {
            desktop.on('council_update', onCouncilUpdate);
            listenerBound = true;
        }

        this.isOpen = true;
        renderLoading('Loading…');
        loadSnapshot(this.sitting, null);
    }

    close() {
        if (!this.isOpen) return;
        stopTimer();
        closeReader();
        if (this.winbox) {
            const wb = this.winbox;
            this.winbox = null;
            wb.close();
        }
        desktop.unregisterWindow(this.id);
        container = null;
        this.isOpen = false;
        if (this.onCloseCallback) this.onCloseCallback(this);
    }

    focus() { if (this.winbox) this.winbox.focus(); }
    minimize() { if (this.winbox) this.winbox.minimize(); }
    restore() { if (this.winbox) this.winbox.restore(); }
    get isMinimized() { return this.winbox ? this.winbox.min : false; }

    /** Switch the board to a different sitting without recreating the window. */
    showSitting(sitting) {
        if (sitting && sitting !== state.sitting) loadSnapshot(sitting, null);
    }
}
