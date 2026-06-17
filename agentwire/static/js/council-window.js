/**
 * Council seating board — a WinBox window that makes a live sitting legible at
 * a glance: each lens (soul) as a tile, the prompt centered above, every tile
 * showing only its *filed verdict*. Tiles swap once, cleanly, when a reply
 * lands (the `council_update` WS delta) — no token streaming, no flicker.
 *
 * Phase 3 applies the approved hi-fi design (council-design-reference.html):
 * the whole tile treatment keys off a single status class
 * `council-tile--{pending,acked,answered,passed,stalled}`, styled in
 * desktop.css. This module owns the *real* data path and renders that markup —
 * the mock META/ROSTER/ROUNDS demo harness from the reference is NOT shipped.
 *
 * Data flow:
 *   - first paint / round switch / reconnect → GET /api/council/live (snapshot)
 *   - per-reply deltas → desktop 'council_update' { sitting, prompt_id, tile }
 *     (or { reset:true } when a new round starts → refetch); the tile that just
 *     filed gets the `council-tile--flip` swap animation.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';

let activeWindow = null;

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
};

let container = null;
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
        renderEmpty(res.status === 409
            ? 'Multiple council sittings — pick one.'
            : 'No live council sitting.');
        return;
    }
    applySnapshot(await res.json());
}

function applySnapshot(snap) {
    state.sitting = snap.sitting;
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

// ── Delta handling (the show) ─────────────────────────────────────────────────

function onCouncilUpdate(msg) {
    if (!activeWindow) return;
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
    return `
        <div class="council-tile council-tile--${esc(status)}" data-soul="${esc(tile.soul)}">
            <div class="council-tile-head">
                <div class="council-tile-name">${esc(tile.soul)}</div>
            </div>
            <div class="council-tile-status">
                <span class="council-chip"><span class="council-chip-dot"></span>${esc(CHIP[status] || status)}</span>
                ${metaHtml}
            </div>
            <div class="council-tile-body">${esc(body)}</div>
            <div class="council-tile-expand">Read full verdict <span>→</span></div>
        </div>`;
}

function counterText() {
    const final = state.roster.filter((s) => isFinal(state.tiles.get(s)?.status)).length;
    return `${final} of ${state.roster.length} in`;
}

function dotsHtml() {
    return state.roster.map((s) => {
        const st = state.tiles.get(s)?.status;
        let cls = 'council-dot-cell';
        if (st === 'stalled') cls += ' council-dot-cell--bad';
        else if (isFinal(st)) cls += ' council-dot-cell--in';
        else if (st === 'acked') cls += ' council-dot-cell--work';
        return `<div class="${cls}"></div>`;
    }).join('');
}

function selectorHtml() {
    if (state.promptIds.length <= 1) return '';
    const opts = state.promptIds.slice().reverse().map((id) => {
        const isLatest = id === state.latestPromptId;
        const tag = `ROUND ${pad2(id)}${isLatest ? ' · LATEST' : ''}`;
        const txt = state.roundTexts[id] || (isLatest ? 'latest round' : `prompt #${id}`);
        return `<div class="council-round-opt ${id === state.promptId ? 'council-round-opt--active' : ''}" data-round="${id}">
            <div class="council-round-tag">${esc(tag)}</div>
            <div class="council-round-txt">${esc(txt)}</div>
        </div>`;
    }).join('');
    return `
        <div class="council-selector">
            <div class="council-selector-btn" data-action="round-toggle">
                <div>
                    <div class="council-sel-lbl">ROUND</div>
                    <div class="council-sel-val">${esc(pad2(state.promptId))}</div>
                </div>
                <div class="council-sel-chev">▼</div>
            </div>
            <div class="council-dropdown">${opts}</div>
        </div>`;
}

function questionHtml() {
    if (!state.promptText) {
        return `<div class="council-q council-q--empty">No prompt asked yet.</div>`;
    }
    return `<div class="council-q"><span class="council-quote">“</span>${esc(state.promptText)}<span class="council-quote">”</span></div>`;
}

function render() {
    if (!container) return;
    if (!state.sitting) { renderEmpty('No live council sitting.'); return; }
    const trio = state.roster.length <= 3 ? ' council-grid--trio' : '';
    const live = state.promptId === state.latestPromptId;
    container.innerHTML = `
        <div class="council-glow"></div>
        <div class="council-vignette"></div>
        <div class="council-shell">
            <div class="council-header">
                <div>
                    <div class="council-brand"><div class="council-led"></div><div class="council-eyebrow">AGENTWIRE · COUNCIL</div></div>
                    <div class="council-sitting">${esc(state.sitting)}</div>
                    ${sittingSelectorInline()}
                </div>
                <div class="council-header-right">
                    <div class="council-counter-wrap">
                        <div class="council-dots">${dotsHtml()}</div>
                        <div class="council-counter">${esc(counterText())}</div>
                    </div>
                    ${selectorHtml()}
                </div>
            </div>
            <div class="council-question-wrap">
                <div class="council-question">
                    <div class="council-kicker">${live ? 'THE QUESTION BEFORE THE COUNCIL' : 'AN EARLIER ROUND'}</div>
                    ${questionHtml()}
                </div>
            </div>
            <div class="council-panel">
                <div class="council-panel-glow"></div>
                <div class="council-grid${trio}">
                    ${state.roster.map((s) => tileHtml(s)).join('')}
                </div>
            </div>
        </div>`;
    bindBoard();
    startTimer();
    if (allFinal()) markCompleteStatic();
}

/** A native <select> to switch sittings when more than one is live. */
function sittingSelectorInline() {
    if (state.sittings.length <= 1) return '';
    const opts = state.sittings
        .map((n) => `<option value="${esc(n)}" ${n === state.sitting ? 'selected' : ''}>${esc(n)}</option>`)
        .join('');
    return `<select class="council-round-select" data-action="sitting" style="margin-top:8px">${opts}</select>`;
}

function renderEmpty(message) {
    if (!container) return;
    stopTimer();
    const picker = state.sittings.length > 1
        ? `<select class="council-round-select" data-action="sitting">${state.sittings.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}</select>`
        : '';
    container.innerHTML = `
        <div class="council-glow"></div>
        <div class="council-shell council-shell--empty">
            ${picker}
            <div class="council-empty">${esc(message)}</div>
        </div>`;
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => loadSnapshot(e.target.value, null));
}

function updateCounter() {
    const dots = container?.querySelector('.council-dots');
    if (dots) dots.innerHTML = dotsHtml();
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

function bindBoard() {
    // Round dropdown
    const selBtn = container.querySelector('[data-action="round-toggle"]');
    const dropdown = container.querySelector('.council-dropdown');
    if (selBtn && dropdown) {
        selBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            dropdown.classList.toggle('council-dropdown--open');
        });
        dropdown.querySelectorAll('.council-round-opt').forEach((opt) => {
            opt.addEventListener('click', () => {
                dropdown.classList.remove('council-dropdown--open');
                loadSnapshot(state.sitting, Number(opt.dataset.round));
            });
        });
    }
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => loadSnapshot(e.target.value, null));
    container.querySelectorAll('.council-tile').forEach(bindTile);
}

function bindTile(el) {
    const soul = el.dataset.soul;
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

// ── Entry point ────────────────────────────────────────────────────────────────

let listenerBound = false;

export function openCouncilWindow(sitting = null) {
    ensureFonts();
    if (activeWindow && activeWindow.window) {
        try {
            activeWindow.focus();
            if (sitting && sitting !== state.sitting) loadSnapshot(sitting, null);
            return;
        } catch (_) { /* fall through and recreate */ }
    }
    container = document.createElement('div');
    container.className = 'council-window-mount';
    activeWindow = new WinBox({
        title: 'Council board',
        icon: '<span style="font-size:14px">🏛️</span>',
        mount: container,
        width: '74%',
        height: '82%',
        minwidth: 480,
        minheight: 380,
        class: ['council-window'],
        onclose: () => {
            stopTimer();
            activeWindow = null;
            return false;
        },
    });

    if (!listenerBound) {
        desktop.on('council_update', onCouncilUpdate);
        listenerBound = true;
    }

    // Close the round dropdown on any outside click.
    document.addEventListener('click', () => {
        container?.querySelector('.council-dropdown--open')?.classList.remove('council-dropdown--open');
    });

    renderEmpty('Loading…');
    loadSnapshot(sitting, null);
}
