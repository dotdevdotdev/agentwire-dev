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
import * as browserStt from './voice/browser-stt.js';
import * as browserTts from './voice/browser-tts.js';
import { voicePromptWrap } from './voice/prompt.js';

const SESSION_KEY = 'agentwire_mobile_session';

const els = {
    sessionList: document.getElementById('sessionList'),
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
let pttState = 'idle'; // idle | recording | processing
let sttCancelled = false;
let mediaRecorder = null;
let audioChunks = [];

// ---------------------------------------------------------------------------
// Status + transcript
// ---------------------------------------------------------------------------

function setStatus(text, isError = false) {
    els.status.textContent = text;
    els.status.classList.toggle('error', isError);
}

function addEntry(kind, text, label = null) {
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

function setSpeaking(speaking) {
    els.voiceIndicator.classList.toggle('speaking', speaking);
    els.voiceIndicator.title = speaking ? 'Speaking' : 'Idle';
}

// ---------------------------------------------------------------------------
// Session picker
// ---------------------------------------------------------------------------

async function loadSessions() {
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

    renderSessions();
    if (pick && pick !== selectedSession) selectSession(pick);
    else if (!pick) setStatus('No sessions running — start one from the desktop', true);
}

function renderSessions() {
    els.sessionList.innerHTML = '';
    if (sessions.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'mobile-empty';
        empty.textContent = 'No local sessions.';
        els.sessionList.appendChild(empty);
        return;
    }
    for (const s of sessions) {
        const btn = document.createElement('button');
        btn.className = 'mobile-session' + (s.name === selectedSession ? ' selected' : '');
        btn.dataset.name = s.name;

        const dot = document.createElement('span');
        dot.className = 'mobile-session-activity' + (s.activity ? ' active' : '');
        const name = document.createElement('span');
        name.className = 'mobile-session-name';
        name.textContent = s.name;
        btn.append(dot, name);

        btn.addEventListener('click', () => selectSession(s.name));
        els.sessionList.appendChild(btn);
    }
}

function selectSession(name) {
    selectedSession = name;
    try { localStorage.setItem(SESSION_KEY, name); } catch {}
    for (const btn of els.sessionList.querySelectorAll('.mobile-session')) {
        btn.classList.toggle('selected', btn.dataset.name === name);
    }
    els.ptt.disabled = false;
    setStatus(`Talking to ${name}`);
    connectSessionWs(name);
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
        switch (msg.type) {
            case 'tts_start':
                // The reply text — show it whether playback is audio or speech
                if (msg.text) addEntry('session', msg.text, msg.session || name);
                setSpeaking(true);
                break;
            case 'speak_text':
                if (msg.text) {
                    setSpeaking(true);
                    browserTts.speak(msg.text, { onEnd: () => setSpeaking(false) });
                }
                break;
            case 'audio':
                if (msg.data) queueAudio(msg.data);
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

function usesBrowserStt() {
    return !['cloud', 'custom'].includes(voiceStatus?.stt?.backend);
}

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

async function startRecording() {
    if (pttState !== 'idle' || !selectedSession) return;
    hideEditBar();

    if (usesBrowserStt()) {
        if (!browserStt.isSupported()) {
            setStatus('No speech recognition in this browser — set stt.backend: cloud or custom for phone STT', true);
            return;
        }
        sttCancelled = false;
        const ok = browserStt.start({
            onFinal: (text) => {
                setPttState('idle');
                if (!sttCancelled && text) showEditBar(text);
            },
            onError: (err) => {
                setStatus(`Speech recognition failed: ${err}`, true);
                setPttState('idle');
            },
        }, voiceStatus?.corrections || {});
        if (ok) setPttState('recording');
        return;
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus'
            : (MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '');
        mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        };
        mediaRecorder.onstop = () => {
            stream.getTracks().forEach(track => track.stop());
            if (audioChunks.length > 0 && pttState === 'processing') {
                transcribeUpload(new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' }));
            }
        };
        mediaRecorder.start();
        setPttState('recording');
    } catch (err) {
        console.error('[Mobile] Failed to start recording:', err);
        setStatus('Microphone access denied', true);
        setPttState('idle');
    }
}

function stopRecording() {
    if (pttState !== 'recording') return;
    setPttState('processing');
    if (usesBrowserStt()) {
        browserStt.stop(); // onFinal fires from onend
        return;
    }
    mediaRecorder?.stop();
}

function cancelRecording() {
    if (usesBrowserStt()) {
        sttCancelled = true;
        browserStt.stop();
        setPttState('idle');
        return;
    }
    audioChunks = [];
    mediaRecorder?.stop();
    setPttState('idle');
}

async function transcribeUpload(blob) {
    try {
        const formData = new FormData();
        formData.append('audio', blob, 'recording.webm');
        const res = await apiFetch('/transcribe', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        const text = data.text?.trim();
        if (text) showEditBar(text);
        else setStatus('No speech detected', true);
    } catch (err) {
        console.error('[Mobile] Transcription failed:', err);
        setStatus(err.message || 'Transcription failed', true);
    } finally {
        setPttState('idle');
    }
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
    addEntry('you', text, 'You');
    setStatus(`Sending to ${selectedSession}…`);
    try {
        const res = await apiFetch(`/send/${selectedSession}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: voicePromptWrap(text) }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        setStatus(`Talking to ${selectedSession}`);
    } catch (err) {
        console.error('[Mobile] Send failed:', err);
        addEntry('error', err.message || 'Send failed');
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
    els.refresh.addEventListener('click', loadSessions);

    // voice-status first: on a fresh device this 401s and raises the token
    // modal; after entry the page reloads with credentials.
    await loadVoiceStatus();
    await loadSessions();
}

init();
