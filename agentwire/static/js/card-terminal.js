/**
 * card-terminal.js
 *
 * Shared "mount voice/terminal onto a topology card's header" plumbing (#778
 * extraction) — previously lived only as WorkspaceWindow._mountCardTerminal
 * (#763), duplicated here would have meant two copies of the record →
 * transcribe → auto-send/edit-before-send pipeline. Now both the Session
 * Workspace window (workspace-window.js) and the Session HUD controller
 * (session-hud-controller.js) import from here.
 *
 * Two mount shapes, one shared mic:
 *   - `mountCardTerminal` — full mini-terminal (TerminalPane) + mic + open-full
 *     button, for an interactive card's expand slot.
 *   - `mountSelfMic` — header-only mic, no terminal, no open button, for the
 *     Session HUD's dimmed "you-are-here" root card (the user is already in
 *     that session's own window — there's nothing to peek at, but its PTT
 *     should still be one tap away).
 * `wirePttMic` is the record/transcribe/send plumbing both share; only where
 * status is surfaced (a TerminalPane's status bar vs the mic button's title)
 * and where the edit-before-send transcript bar is anchored differ.
 */

import { desktop } from './desktop-manager.js';
import { TerminalPane } from './terminal-pane.js';
import { PttController } from './ptt.js';
import { apiFetch } from './api.js';
import { buildSessionId, normalizeMachine } from './session-id.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';

/**
 * Wire a titlebar-style PTT mic button: press-and-hold to record, transcribe,
 * then auto-send or show an edit-before-send bar. Shared by `mountCardTerminal`
 * and `mountSelfMic` below.
 *
 * @param {string} name - Session name
 * @param {object} session - Session record (for machine)
 * @param {Object} opts
 * @param {(bar: HTMLElement) => void} opts.insertBar - Insert the transcript-edit bar into the DOM
 * @param {(kind: string, message: string) => void} opts.setStatus - Status sink
 * @param {() => void} [opts.afterRemove] - Called whenever the transcript bar is dismissed/sent
 * @returns {{ pttBtn: HTMLButtonElement, dispose: () => void }}
 */
export function wirePttMic(name, session, { insertBar, setStatus, afterRemove }) {
    const machine = normalizeMachine(session?.machine);
    const sessionId = buildSessionId(name, machine);

    const pttBtn = document.createElement('button');
    pttBtn.type = 'button';
    pttBtn.className = 'wb-title-ptt';
    pttBtn.title = 'Hold to record voice input';
    pttBtn.innerHTML = '<span class="ptt-icon">🎤</span>';

    let transcriptBar = null;
    const removeTranscriptBar = () => {
        transcriptBar?.remove();
        transcriptBar = null;
        afterRemove?.();
    };

    const sendVoiceText = async (text) => {
        try {
            const res = await apiFetch(`/send/${sessionId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: voicePromptWrap(text) }),
            });
            const data = await res.json();
            if (data.error) throw new Error(data.error);
            setStatus('connected', `Sent: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`);
        } catch (err) {
            console.error('[card-terminal] Voice send failed:', err);
            setStatus('error', err.message || 'Voice input failed');
        }
    };

    const showTranscriptBar = (text) => {
        removeTranscriptBar();
        const bar = document.createElement('div');
        bar.className = 'wb-transcript-bar';
        bar.innerHTML = `
            <input type="text" class="wb-transcript-input" />
            <button class="wb-transcript-send" title="Send (Enter)">➤</button>
            <button class="wb-transcript-dismiss" title="Discard (Esc)">✕</button>
        `;
        const input = bar.querySelector('.wb-transcript-input');
        input.value = text;

        const send = () => {
            const value = input.value.trim();
            removeTranscriptBar();
            if (value) sendVoiceText(value);
        };
        bar.querySelector('.wb-transcript-send').addEventListener('click', send);
        bar.querySelector('.wb-transcript-dismiss').addEventListener('click', removeTranscriptBar);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); send(); }
            else if (e.key === 'Escape') { e.preventDefault(); removeTranscriptBar(); }
            e.stopPropagation();
        });

        insertBar(bar);
        transcriptBar = bar;
        input.focus();
        input.select();
    };

    const ptt = new PttController({
        getVoiceStatus: () => desktop.voiceStatus,
        onState: (state) => {
            pttBtn.classList.remove('recording', 'processing');
            if (state === 'recording') {
                pttBtn.classList.add('recording');
                pttBtn.querySelector('.ptt-icon').textContent = '🔴';
            } else if (state === 'processing') {
                pttBtn.classList.add('processing');
                pttBtn.querySelector('.ptt-icon').textContent = '🎤';
            } else {
                pttBtn.querySelector('.ptt-icon').textContent = '🎤';
            }
        },
        onResult: (text) => {
            if (isAutoSend()) sendVoiceText(text);
            else showTranscriptBar(text);
        },
        onError: (kind, message) => setStatus('error', message),
    });

    // Same pointer-capture pattern as SessionWindow's titlebar PTT button.
    const onDown = (e) => {
        e.preventDefault();
        e.stopPropagation();
        pttBtn.setPointerCapture?.(e.pointerId);
        ptt.start();
    };
    const onUp = (e) => {
        e.stopPropagation();
        pttBtn.releasePointerCapture?.(e.pointerId);
        if (ptt.state === 'recording') ptt.stop();
    };
    const onCancel = (e) => {
        pttBtn.releasePointerCapture?.(e.pointerId);
        if (ptt.state === 'recording') ptt.cancel();
    };
    pttBtn.addEventListener('pointerdown', onDown, true);
    pttBtn.addEventListener('pointerup', onUp, true);
    pttBtn.addEventListener('pointercancel', onCancel);

    return {
        pttBtn,
        dispose: () => removeTranscriptBar(),
    };
}

/**
 * Mount a mini-terminal (#763) into a card's expand slot: a TerminalPane over
 * the session's real WS, plus a titlebar-style mic button wired the same way
 * SessionWindow's PTT is. Returns a dispose function TopologyView calls on
 * collapse/prune/view-teardown.
 *
 * @param {string} name - Session name
 * @param {object} session - Session record (for machine)
 * @param {HTMLElement} slotEl - Empty slot appended into the card
 * @param {Object} [opts]
 * @param {import('./topology-render.js').TopologyView} [opts.topologyView] - Notified
 *   (via `collapseCard`) if the session's terminal reports the underlying session ended
 * @returns {() => void} cleanup
 */
export function mountCardTerminal(name, session, slotEl, { topologyView } = {}) {
    const machine = normalizeMachine(session?.machine);

    // Actions (mic + open-full) live in the card's own header row next to
    // the role chip — not a dedicated toolbar row — so the mini-terminal
    // reclaims that vertical space. topology-render.js's card-click collapse
    // toggle exempts .topology-card-actions the same way it exempts the slot.
    const card = slotEl.closest('.topology-card');
    const headerRow = card?.querySelector('.topology-card-top');
    const actions = document.createElement('div');
    actions.className = 'topology-card-actions';

    const termHost = document.createElement('div');
    termHost.className = 'topology-card-mini-terminal';
    slotEl.append(termHost);

    const pane = new TerminalPane(termHost, {
        session: name,
        machine,
        // A card is a compact peek — render smaller than the global terminal
        // pref (16/20px) so more rows are visible; the ⤢ pop-out opens the
        // full-size window for real work.
        fontSize: 14,
        onSessionEnded: () => topologyView?.collapseCard(name),
    });

    const { pttBtn, dispose: disposePtt } = wirePttMic(name, session, {
        insertBar: (bar) => termHost.before(bar),
        setStatus: (kind, message) => {
            pane.setStatus(kind, message);
            if (kind === 'connected') setTimeout(() => pane.setStatus('connected', 'Connected'), 3000);
        },
        afterRemove: () => pane.focus(),
    });

    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'topology-card-mini-open';
    openBtn.title = 'Open full terminal';
    openBtn.textContent = '⤢';
    openBtn.addEventListener('click', async () => {
        const { openSessionTerminal } = await import('./desktop.js');
        openSessionTerminal(name, 'terminal', machine);
    });

    actions.append(pttBtn, openBtn);
    headerRow?.appendChild(actions);

    return () => {
        disposePtt();
        actions.remove();
        pane.dispose();
    };
}

/**
 * Mount a header-only PTT mic for a card that never gets a mini-terminal —
 * the Session HUD's you-are-here self/root card (#778): the user is already
 * inside that session's own window, so there's nothing to peek at, but its
 * mic should still be one tap away. Shares `wirePttMic`'s record/transcribe/
 * send plumbing with `mountCardTerminal`; status surfaces on the mic
 * button's title (no TerminalPane to show it on) and the transcript-edit bar
 * lands directly under the header (no terminal to sit above).
 *
 * @param {string} name - Session name
 * @param {object} session - Session record (for machine)
 * @param {HTMLElement} card - The topology card element (already in the DOM)
 * @returns {() => void} cleanup
 */
export function mountSelfMic(name, session, card) {
    const headerRow = card?.querySelector('.topology-card-top');
    const actions = document.createElement('div');
    actions.className = 'topology-card-actions';

    const { pttBtn, dispose: disposePtt } = wirePttMic(name, session, {
        insertBar: (bar) => headerRow?.after(bar),
        setStatus: (kind, message) => {
            pttBtn.title = message;
            if (kind === 'connected') {
                setTimeout(() => { pttBtn.title = 'Hold to record voice input'; }, 3000);
            }
        },
    });

    actions.append(pttBtn);
    headerRow?.appendChild(actions);

    return () => {
        disposePtt();
        actions.remove();
    };
}
