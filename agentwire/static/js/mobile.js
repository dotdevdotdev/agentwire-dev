/**
 * Mobile PTT page (#279) — pick session → hold to talk → transcript → send.
 *
 * A standalone phone surface, not the WinBox desktop. Auth follows the portal
 * convention: the page shell is public, every API call goes through apiFetch
 * (bearer token + 401 token modal) and the session WebSocket authenticates
 * via the wsProtocols() subprotocol.
 *
 * Voice tiers mirror the desktop session window:
 *   - default STT: browser SpeechRecognition (Chrome) — no audio upload
 *   - cloud/custom STT: MediaRecorder → POST /transcribe
 * TTS replies arrive over the per-session WS (smart routing treats this page
 * as a connected client): `speak_text` (browser synthesis) or `audio` (WAV).
 */

import { apiFetch, wsProtocols } from './api.js';
import { attachHorizontalSwipe } from './utils/swipe.js';
import { isService, isCouncil, loadCustomServices } from './service-classification.js';
import { PttController } from './ptt.js';
import * as browserTts from './voice/browser-tts.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';

const SESSION_KEY = 'agentwire_mobile_session';

const els = {
    sessionList: document.getElementById('sessionList'),
    tabSessions: document.getElementById('tabSessions'),
    tabServices: document.getElementById('tabServices'),
    refresh: document.getElementById('refreshSessions'),
    transcript: document.getElementById('transcript'),
    editBar: document.getElementById('editBar'),
    editInput: document.getElementById('editInput'),
    editSend: document.getElementById('editSend'),
    editDismiss: document.getElementById('editDismiss'),
    status: document.getElementById('statusLine'),
    ptt: document.getElementById('pttButton'),
    pttIcon: document.querySelector('.mobile-ptt-icon'),
    pttLabel: document.querySelector('.mobile-ptt-label'),
    voiceIndicator: document.getElementById('voiceIndicator'),
};

let voiceStatus = null;
let sessions = [];
let selectedSession = null;
let activeTab = 'sessions'; // 'sessions' | 'services' — classification shared with the desktop sidebar
let pttState = 'idle'; // idle | recording | processing (mirrors ptt controller)

// ---------------------------------------------------------------------------
// Status + transcript
// ---------------------------------------------------------------------------

function setStatus(text, isError = false) {
    els.status.textContent = text;
    els.status.classList.toggle('error', isError);
}

// Per-session conversations (#289): entries are keyed by session and the
// visible transcript follows the selection. In-memory only — survives session
// switches, not reloads.
const transcripts = new Map(); // session name → [{kind, text, label}]

function addEntry(session, kind, text, label = null) {
    if (!transcripts.has(session)) transcripts.set(session, []);
    transcripts.get(session).push({ kind, text, label });
    if (session === selectedSession) appendEntryEl(kind, text, label);
}

function appendEntryEl(kind, text, label) {
    const entry = document.createElement('div');
    entry.className = `mobile-entry mobile-entry-${kind}`;
    if (label) {
        const labelEl = document.createElement('span');
        labelEl.className = 'mobile-entry-label';
        labelEl.textContent = label;
        entry.appendChild(labelEl);
    }
    entry.appendChild(document.createTextNode(text));
    els.transcript.appendChild(entry);
    els.transcript.scrollTop = els.transcript.scrollHeight;
}

function renderTranscript() {
    els.transcript.innerHTML = '';
    for (const e of transcripts.get(selectedSession) || []) {
        appendEntryEl(e.kind, e.text, e.label);
    }
}

function setSpeaking(speaking) {
    els.voiceIndicator.classList.toggle('speaking', speaking);
    els.voiceIndicator.title = speaking ? 'Speaking' : 'Idle';
}

// ---------------------------------------------------------------------------
// Session picker
// ---------------------------------------------------------------------------

async function loadSessions() {
    const firstLoad = selectedSession === null;
    try {
        const res = await apiFetch('/api/sessions/local');
        const data = await res.json();
        sessions = data.sessions || [];
    } catch {
        sessions = [];
    }

    // The voice orchestrator first, the rest alphabetical
    sessions.sort((a, b) => {
        if (a.name === 'agentwire') return -1;
        if (b.name === 'agentwire') return 1;
        return a.name.localeCompare(b.name);
    });

    const stored = localStorage.getItem(SESSION_KEY);
    const names = sessions.map(s => s.name);
    const pick = names.includes(selectedSession) ? selectedSession
        : names.includes(stored) ? stored
        : names.includes('agentwire') ? 'agentwire'
        : names[0] || null;

    // On first load, open the tab holding the restored selection so the
    // highlighted card is visible (e.g. last talked to a service session).
    if (firstLoad && pick) setTab(isService(pick) ? 'services' : 'sessions', { render: false });

    renderSessions();
    if (pick && pick !== selectedSession) selectSession(pick);
    else if (!pick) setStatus('No sessions running — start one from the desktop', true);
}

function setTab(tab, { render = true } = {}) {
    activeTab = tab;
    for (const btn of [els.tabSessions, els.tabServices]) {
        const selected = btn.dataset.tab === tab;
        btn.classList.toggle('selected', selected);
        btn.setAttribute('aria-selected', String(selected));
    }
    if (render) renderSessions();
}

// The sessions shown on the active tab, in display order — the SSOT for both
// the rendered list and two-finger swipe cycling. Council sessions have no home
// on mobile yet, so they stay out of both tabs.
function visibleSessions() {
    return sessions.filter(s => !isCouncil(s.name || '') && isService(s.name || '') === (activeTab === 'services'));
}

function renderSessions() {
    els.sessionList.innerHTML = '';
    const visible = visibleSessions();
    if (visible.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'mobile-empty';
        empty.textContent = activeTab === 'services' ? 'No services.' : 'No local sessions.';
        els.sessionList.appendChild(empty);
        return;
    }
    for (const s of visible) {
        const state = s.state || 'off';
        const btn = document.createElement('button');
        btn.className = `mobile-session state-${state}` + (s.name === selectedSession ? ' selected' : '');
        btn.dataset.name = s.name;

        const row = document.createElement('span');
        row.className = 'mobile-session-row';
        const dot = document.createElement('span');
        dot.className = 'mobile-session-state';
        const name = document.createElement('span');
        name.className = 'mobile-session-name';
        name.textContent = s.name;
        row.append(dot, name);
        btn.append(row);

        // One-line hint: what a blocked session is waiting on; off cards
        // stay tappable but say why they're muted.
        const hint = state === 'needs_input' ? (s.state_hint || 'Waiting on you')
            : state === 'off' ? 'no agent running'
            : null;
        if (hint) {
            const hintEl = document.createElement('span');
            hintEl.className = 'mobile-session-hint';
            hintEl.textContent = hint;
            btn.append(hintEl);
        }

        btn.addEventListener('click', () => selectSession(s.name));
        els.sessionList.appendChild(btn);
    }
    updateHeaderState();
}

// Mirror the selected session's state on the "Talking to X" status line (#290)
const SESSION_STATES = ['working', 'idle', 'needs_input', 'off'];

function updateHeaderState() {
    const s = sessions.find(x => x.name === selectedSession);
    const state = s ? (s.state || 'off') : 'off';
    for (const st of SESSION_STATES) {
        els.status.classList.toggle(`state-${st}`, st === state);
    }
}

function selectSession(name) {
    if (name !== selectedSession) {
        // Pending audio belongs to the previous session — drop it.
        audioQueue.length = 0;
        setSpeaking(false);
    }
    selectedSession = name;
    try { localStorage.setItem(SESSION_KEY, name); } catch {}
    for (const btn of els.sessionList.querySelectorAll('.mobile-session')) {
        btn.classList.toggle('selected', btn.dataset.name === name);
    }
    els.ptt.disabled = false;
    setStatus(`Talking to ${name}`);
    updateHeaderState();
    renderTranscript();
    connectSessionWs(name);
}

// Move to the next (+1) / previous (-1) session on the active tab, wrapping
// around — the mobile analog of Tab/Shift+Tab on the desktop.
function cycleSession(direction) {
    const visible = visibleSessions();
    if (visible.length < 2) return;
    const cur = visible.findIndex(s => s.name === selectedSession);
    const next = visible[(cur + direction + visible.length) % visible.length];
    if (next && next.name !== selectedSession) selectSession(next.name);
}

// Single-finger horizontal swipe to cycle sessions — the mobile analog of
// Tab/Shift+Tab. Shared detector (utils/swipe.js); `touch-action: pan-y` on
// .mobile-app (mobile.css) keeps vertical scroll native. PTT + edit bar opt out
// so press-to-talk and typing are untouched.
function setupSwipeCycling() {
    const surface = document.querySelector('.mobile-app') || document;
    attachHorizontalSwipe(surface, cycleSession, {
        ignore: (t) => !!(t.closest && (t.closest('#pttButton') || t.closest('#editBar'))),
    });
}

// ---------------------------------------------------------------------------
// Per-session WebSocket — TTS replies (speak_text / audio) + tts_start text
// ---------------------------------------------------------------------------

let ws = null;
let wsReconnectTimer = null;
let wsReconnectDelay = 1000;
let tokenProbeFired = false;

function connectSessionWs(name) {
    clearTimeout(wsReconnectTimer);
    if (ws) {
        ws.onclose = null;
        try { ws.close(); } catch {}
        ws = null;
    }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket;
    try {
        socket = new WebSocket(`${protocol}//${location.host}/ws/${name}`, wsProtocols());
    } catch {
        scheduleWsReconnect(name);
        return;
    }
    ws = socket;

    let opened = false;
    socket.onopen = () => {
        opened = true;
        wsReconnectDelay = 1000;
    };
    socket.onmessage = (event) => {
        let msg;
        try { msg = JSON.parse(event.data); } catch { return; }
        // Messages land in the transcript of the session they belong to —
        // never blindly into whatever view is open (#289).
        const from = msg.session || name;
        switch (msg.type) {
            case 'tts_start':
                // The reply text — show it whether playback is audio or speech
                if (msg.text) addEntry(from, 'session', msg.text, from);
                if (from === selectedSession) setSpeaking(true);
                break;
            case 'speak_text':
                if (msg.text && from === selectedSession) {
                    setSpeaking(true);
                    browserTts.speak(msg.text, { onEnd: () => setSpeaking(false) });
                }
                break;
            case 'audio':
                if (msg.data && from === selectedSession) queueAudio(msg.data);
                break;
            // `output` (terminal polling) and lock messages are irrelevant here
        }
    };
    socket.onclose = () => {
        if (ws !== socket) return;
        ws = null;
        // Handshake never completed — likely a 401. Probe the API once so
        // apiFetch raises the token modal (page reloads after entry).
        if (!opened && !tokenProbeFired) {
            tokenProbeFired = true;
            apiFetch('/api/sessions/local').catch(() => {});
        }
        scheduleWsReconnect(name);
    };
}

function scheduleWsReconnect(name) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = setTimeout(() => {
        if (selectedSession === name) connectSessionWs(name);
    }, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * 2, 15000);
}

// ---------------------------------------------------------------------------
// Audio playback (custom-tier WAV chunks) — sequential queue, unlocked by
// the first PTT press (iOS requires a user gesture before audio can play)
// ---------------------------------------------------------------------------

let audioContext = null;
const audioQueue = [];
let audioPlaying = false;

function ensureAudioContext() {
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioContext.state === 'suspended') {
        audioContext.resume().catch(() => {});
    }
    return audioContext;
}

function queueAudio(base64Data) {
    audioQueue.push(base64Data);
    if (!audioPlaying) playNextAudio();
}

async function playNextAudio() {
    const base64Data = audioQueue.shift();
    if (!base64Data) {
        audioPlaying = false;
        setSpeaking(false);
        return;
    }
    audioPlaying = true;
    setSpeaking(true);
    try {
        const binary = atob(base64Data);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

        const ctx = ensureAudioContext();
        const buffer = await ctx.decodeAudioData(bytes.buffer);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        source.onended = () => playNextAudio();
        source.start(0);
    } catch (err) {
        console.error('[Mobile] Audio playback failed:', err);
        playNextAudio();
    }
}

// ---------------------------------------------------------------------------
// PTT — pointer events with capture, same pattern as the titlebar PTT
// ---------------------------------------------------------------------------

function setupPtt() {
    const onDown = (e) => {
        e.preventDefault();
        e.stopPropagation();
        ensureAudioContext(); // unlock playback while we have a user gesture
        els.ptt.setPointerCapture?.(e.pointerId);
        startRecording();
    };
    const onUp = (e) => {
        e.stopPropagation();
        els.ptt.releasePointerCapture?.(e.pointerId);
        if (pttState === 'recording') stopRecording();
    };
    const onCancel = (e) => {
        els.ptt.releasePointerCapture?.(e.pointerId);
        if (pttState === 'recording') cancelRecording();
    };
    els.ptt.addEventListener('pointerdown', onDown, true);
    els.ptt.addEventListener('pointerup', onUp, true);
    els.ptt.addEventListener('pointercancel', onCancel);
    // A long-press context menu would break the hold mid-recording
    els.ptt.addEventListener('contextmenu', (e) => e.preventDefault());
}

const ptt = new PttController({
    getVoiceStatus: () => voiceStatus,
    onState: setPttState,
    onResult: (text) => {
        if (isAutoSend()) sendText(text);
        else showEditBar(text);
    },
    onError: (kind, message) => {
        if (kind === 'unsupported') {
            setStatus('No speech recognition in this browser — set stt.backend: cloud or custom for phone STT', true);
            return;
        }
        setStatus(message, true);
    },
});

function setPttState(state) {
    pttState = state;
    els.ptt.classList.remove('recording', 'processing');
    if (state === 'recording') {
        els.ptt.classList.add('recording');
        els.pttIcon.textContent = '🔴';
        els.pttLabel.textContent = 'Release to send';
    } else if (state === 'processing') {
        els.ptt.classList.add('processing');
        els.pttIcon.textContent = '🎤';
        els.pttLabel.textContent = 'Transcribing…';
    } else {
        els.pttIcon.textContent = '🎤';
        els.pttLabel.textContent = 'Hold to talk';
    }
}

function startRecording() {
    if (pttState !== 'idle' || !selectedSession) return;
    hideEditBar();
    ptt.start();
}

function stopRecording() {
    ptt.stop();
}

function cancelRecording() {
    ptt.cancel();
}

// ---------------------------------------------------------------------------
// Edit-before-send bar + send
// ---------------------------------------------------------------------------

function showEditBar(text) {
    els.editInput.value = text;
    els.editBar.hidden = false;
    els.editInput.focus();
    els.editInput.select();
}

function hideEditBar() {
    els.editBar.hidden = true;
    els.editInput.value = '';
}

async function sendText(text) {
    if (!selectedSession) return;
    // Pin the target: if the user switches sessions while the send is in
    // flight, the entry (and any error) stays with the session it went to.
    const target = selectedSession;
    addEntry(target, 'you', text, 'You');
    setStatus(`Sending to ${target}…`);
    try {
        const res = await apiFetch(`/send/${target}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: voicePromptWrap(text) }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        setStatus(`Talking to ${selectedSession}`);
    } catch (err) {
        console.error('[Mobile] Send failed:', err);
        addEntry(target, 'error', err.message || 'Send failed');
        setStatus(`Talking to ${selectedSession}`);
    }
}

function setupEditBar() {
    const send = () => {
        const value = els.editInput.value.trim();
        hideEditBar();
        if (value) sendText(value);
    };
    els.editSend.addEventListener('click', send);
    els.editDismiss.addEventListener('click', hideEditBar);
    els.editInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); send(); }
        else if (e.key === 'Escape') { e.preventDefault(); hideEditBar(); }
    });
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function loadVoiceStatus() {
    try {
        const res = await apiFetch('/api/voice-status');
        voiceStatus = await res.json();
    } catch {
        // Fail toward the zero-dependency tier (matches desktop-manager)
        voiceStatus = {
            stt: { backend: 'default', available: true },
            tts: { backend: 'default', available: true },
            corrections: {},
        };
    }
}

async function init() {
    setupPtt();
    setupEditBar();
    setupSwipeCycling();
    els.refresh.addEventListener('click', loadSessions);
    els.tabSessions.addEventListener('click', () => setTab('sessions'));
    els.tabServices.addEventListener('click', () => setTab('services'));

    // voice-status first: on a fresh device this 401s and raises the token
    // modal; after entry the page reloads with credentials.
    await loadVoiceStatus();
    // Custom services must be merged before the first render so config-defined
    // services land on the Services tab (same allowlist as the desktop sidebar).
    await loadCustomServices();
    await loadSessions();

    // Keep the state visuals (#290) live — poll-based v1, paused while the
    // page is backgrounded so it stays battery-friendly.
    setInterval(() => {
        if (document.visibilityState === 'visible') loadSessions();
    }, 5000);
}

init();
