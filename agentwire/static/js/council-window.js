/**
 * Council seating board — a WinBox window that makes a live sitting legible at
 * a glance: each lens (soul) as a tile, the prompt centered above, every tile
 * showing only its *filed verdict*. Tiles swap once, cleanly, when a reply
 * lands (the `council_update` WS delta) — no token streaming, no flicker.
 *
 * Data flow:
 *   - first paint / round switch / reconnect → GET /api/council/live (snapshot)
 *   - per-reply deltas → desktop 'council_update' { sitting, prompt_id, tile }
 *     (or { reset:true } when a new round starts → refetch)
 *
 * Status enum drives a per-tile class so the designer can restyle by class
 * without touching this logic:
 *   pending | acked | answered | passed | stalled
 *
 * CORE build (issue #403, Phases 0–2). Phase 3 affordances + final visual
 * styling are intentionally a thin layer on top of these hooks.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';

let activeWindow = null;

// View state — one board at a time (re-opening focuses the existing window).
const state = {
    sitting: null,        // sitting <name> currently shown
    promptId: null,       // prompt id currently shown
    latestPromptId: null, // newest round for the sitting (for "live vs history")
    promptIds: [],        // every round id, ascending (selector)
    promptText: '',
    roster: [],           // fixed soul order — never reordered under the user
    createdAt: '',        // round start (drives the pending elapsed timer)
    tiles: new Map(),     // soul -> { soul, status, kind, verdict, filed_at }
    sittings: [],         // every live sitting (for the sitting picker)
};

let container = null;
let timerInterval = null;

const STATUS_LABEL = {
    pending: 'waiting…',
    acked: 'researching…',
    answered: 'take',
    passed: 'passed',
    stalled: 'no response',
};

const KIND_LABEL = { take: 'take', ack: 'ack', pass: 'pass' };

const CLAMP_CHARS = 200;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
}

/** Whether a tile counts toward "N of M in" (terminal states only). */
function isFinal(status) {
    return status === 'answered' || status === 'passed';
}

function clampVerdict(text) {
    const t = String(text || '').trim();
    if (t.length <= CLAMP_CHARS) return t;
    return t.slice(0, CLAMP_CHARS).trimEnd() + '…';
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

// ── Snapshot fetch ──────────────────────────────────────────────────────────

async function loadSnapshot(sitting, promptId) {
    const params = new URLSearchParams();
    if (sitting) params.set('sitting', sitting);
    if (promptId != null) params.set('prompt_id', String(promptId));
    const qs = params.toString();
    const res = await apiFetch(`/api/council/live${qs ? `?${qs}` : ''}`);
    if (res.status === 409) {
        // Ambiguous — multiple sittings, none chosen. Show the picker.
        const body = await res.json().catch(() => ({}));
        state.sittings = body.sittings || [];
        state.sitting = null;
        renderEmpty('Multiple council sittings — pick one.');
        return;
    }
    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        state.sittings = body.sittings || [];
        state.sitting = null;
        renderEmpty('No live council sitting.');
        return;
    }
    const snap = await res.json();
    applySnapshot(snap);
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
    state.tiles = new Map();
    for (const tile of snap.tiles || []) {
        state.tiles.set(tile.soul, tile);
    }
    render();
}

// ── Delta handling (the show) ───────────────────────────────────────────────

function onCouncilUpdate(msg) {
    if (!activeWindow) return;
    if (msg.sitting !== state.sitting) return;
    // Deltas only apply to the round we're actually viewing — history is static.
    const viewingLatest = state.promptId === state.latestPromptId;

    if (msg.reset) {
        // New round started. If the board is following live, jump to it.
        if (viewingLatest || state.promptId == null) {
            loadSnapshot(state.sitting, null);
        } else {
            // Viewing history — just refresh the round list so the selector
            // shows the new round without yanking the user off their page.
            loadSnapshot(state.sitting, state.promptId);
        }
        return;
    }

    if (!msg.tile) return;
    if (msg.prompt_id !== state.promptId) return;  // delta for a different round

    const prev = state.tiles.get(msg.tile.soul);
    state.tiles.set(msg.tile.soul, msg.tile);
    swapTile(msg.tile, prev);
}

/** Replace a single tile in place with a settle animation — no full re-render. */
function swapTile(tile, prev) {
    const el = container?.querySelector(`[data-soul="${cssEscape(tile.soul)}"]`);
    if (!el) { render(); return; }
    el.outerHTML = tileHtml(tile);
    const fresh = container.querySelector(`[data-soul="${cssEscape(tile.soul)}"]`);
    if (fresh && (!prev || prev.status !== tile.status)) {
        fresh.classList.add('council-tile--settle');
        // eslint-disable-next-line no-unused-expressions
        fresh.offsetWidth;  // reflow so the animation re-triggers
    }
    updateCounter();
    if (allFinal()) markComplete();
}

function allFinal() {
    if (!state.roster.length) return false;
    return state.roster.every((s) => isFinal(state.tiles.get(s)?.status));
}

function cssEscape(s) {
    return String(s).replace(/["\\]/g, '\\$&');
}

// ── Rendering ────────────────────────────────────────────────────────────────

function tileHtml(tile) {
    const status = tile.status || 'pending';
    const kindChip = tile.kind
        ? `<span class="council-chip council-chip--${esc(tile.kind)}">${esc(KIND_LABEL[tile.kind] || tile.kind)}</span>`
        : `<span class="council-chip council-chip--${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;

    let bodyHtml;
    if (status === 'pending' || status === 'stalled') {
        const timer = status === 'pending'
            ? `<span class="council-timer" data-since="${esc(state.createdAt)}">· ${esc(elapsedSince(state.createdAt))}</span>`
            : '';
        const note = status === 'stalled' ? 'no response' : 'waiting for a verdict';
        bodyHtml = `<div class="council-tile-placeholder">${esc(note)} ${timer}</div>`;
    } else {
        bodyHtml = `<div class="council-verdict">${esc(clampVerdict(tile.verdict))}</div>`;
    }

    const expandable = status === 'answered' || status === 'passed' || status === 'acked';
    return `
        <div class="council-tile council-tile--${esc(status)}"
             data-soul="${esc(tile.soul)}"
             ${expandable ? 'data-expandable="1" role="button" tabindex="0"' : ''}>
            <div class="council-tile-head">
                <span class="council-soul">${esc(tile.soul)}</span>
                ${kindChip}
            </div>
            ${bodyHtml}
        </div>`;
}

function counterText() {
    const final = state.roster.filter((s) => isFinal(state.tiles.get(s)?.status)).length;
    return `${final} of ${state.roster.length} in`;
}

function roundSelectorHtml() {
    if (state.promptIds.length <= 1) return '';
    const opts = state.promptIds
        .map((id) => {
            const label = id === state.latestPromptId ? `#${id} (latest)` : `#${id}`;
            return `<option value="${id}" ${id === state.promptId ? 'selected' : ''}>${label}</option>`;
        })
        .join('');
    return `<select class="council-round-select" data-action="round">${opts}</select>`;
}

function sittingSelectorHtml() {
    if (state.sittings.length <= 1) return '';
    const opts = state.sittings
        .map((n) => `<option value="${esc(n)}" ${n === state.sitting ? 'selected' : ''}>${esc(n)}</option>`)
        .join('');
    return `<select class="council-sitting-select" data-action="sitting">${opts}</select>`;
}

function render() {
    if (!container) return;
    if (!state.sitting) { renderEmpty('No live council sitting.'); return; }
    const live = state.promptId === state.latestPromptId;
    container.innerHTML = `
        <div class="council-board" data-complete="0">
            <header class="council-header">
                <div class="council-header-left">
                    <span class="council-sitting-name">${esc(state.sitting)}</span>
                    ${sittingSelectorHtml()}
                </div>
                <div class="council-header-right">
                    ${roundSelectorHtml()}
                    <span class="council-counter">${esc(counterText())}</span>
                </div>
            </header>
            <div class="council-question">
                ${state.promptText
                    ? esc(state.promptText)
                    : '<span class="council-question-empty">No prompt asked yet.</span>'}
                ${live ? '' : '<span class="council-history-badge">history</span>'}
            </div>
            <div class="council-grid">
                ${state.roster.map((s) => tileHtml(state.tiles.get(s) || { soul: s, status: 'pending' })).join('')}
            </div>
        </div>`;
    bindBoard();
    startTimer();
    if (allFinal()) markComplete();
}

function renderEmpty(message) {
    if (!container) return;
    stopTimer();
    container.innerHTML = `
        <div class="council-board council-board--empty">
            ${state.sittings.length > 1 ? `<div class="council-empty-picker">${sittingSelectorHtml()}</div>` : ''}
            <div class="council-empty">${esc(message)}</div>
        </div>`;
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => {
        loadSnapshot(e.target.value, null);
    });
}

function updateCounter() {
    const el = container?.querySelector('.council-counter');
    if (el) el.textContent = counterText();
}

function markComplete() {
    const board = container?.querySelector('.council-board');
    if (board && board.dataset.complete !== '1') {
        board.dataset.complete = '1';
        board.classList.add('council-board--flourish');
    }
}

function bindBoard() {
    container.querySelector('[data-action="round"]')?.addEventListener('change', (e) => {
        loadSnapshot(state.sitting, Number(e.target.value));
    });
    container.querySelector('[data-action="sitting"]')?.addEventListener('change', (e) => {
        loadSnapshot(e.target.value, null);
    });
    container.querySelectorAll('[data-expandable="1"]').forEach((el) => {
        const open = () => openReader(el.dataset.soul);
        el.addEventListener('click', open);
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
    });
}

// ── Reader overlay (click-to-expand full verdict) ─────────────────────────────

function openReader(soul) {
    const tile = state.tiles.get(soul);
    if (!tile || !tile.verdict) return;
    const overlay = document.createElement('div');
    overlay.className = 'council-reader-overlay';
    overlay.innerHTML = `
        <div class="council-reader" role="dialog" aria-label="${esc(soul)} verdict">
            <div class="council-reader-head">
                <span class="council-soul">${esc(soul)}</span>
                <span class="council-chip council-chip--${esc(tile.kind || tile.status)}">${esc(KIND_LABEL[tile.kind] || STATUS_LABEL[tile.status] || tile.status)}</span>
                <button class="council-reader-close" aria-label="Close">×</button>
            </div>
            <div class="council-reader-body">${esc(tile.verdict)}</div>
        </div>`;
    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('.council-reader-close')?.addEventListener('click', close);
    const onKey = (e) => {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
    };
    document.addEventListener('keydown', onKey);
    container.appendChild(overlay);
}

// ── Pending elapsed timer ─────────────────────────────────────────────────────

function startTimer() {
    stopTimer();
    timerInterval = setInterval(() => {
        if (!container) return;
        container.querySelectorAll('.council-timer').forEach((el) => {
            el.textContent = `· ${elapsedSince(el.dataset.since)}`;
        });
    }, 1000);
}

function stopTimer() {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}

// ── Entry point ────────────────────────────────────────────────────────────────

let listenerBound = false;

export function openCouncilWindow(sitting = null) {
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
        width: '70%',
        height: '78%',
        minwidth: 480,
        minheight: 360,
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

    renderEmpty('Loading…');
    loadSnapshot(sitting, null);
}
