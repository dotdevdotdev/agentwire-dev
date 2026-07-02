/**
 * ptt.js — shared push-to-talk pipeline (#631).
 *
 * One implementation of the record → transcribe → deliver flow used by the
 * desktop global PTT (desktop.js), the mobile PTT page (mobile.js), and the
 * per-window PTT (session-window.js). Two tiers, mirroring browser-stt.js:
 *
 *   - browser tier: SpeechRecognition in the browser (Chrome) — no upload
 *   - server tier (cloud/custom/default-with-Moonshine): MediaRecorder →
 *     POST /transcribe
 *
 * The controller owns the state machine (idle → recording → processing →
 * idle), the recorder, and the transcribe call. Surfaces own everything
 * intentionally different per surface: trigger events (pointer capture vs
 * mouse vs Ctrl+Space), what to do with the transcript (auto-send target,
 * edit-before-send bar), and where errors are shown (status line, window
 * status, console).
 */

import { apiFetch } from './api.js';
import * as browserStt from './voice/browser-stt.js';

export class PttController {
    /**
     * @param {Object} opts
     * @param {Function} opts.getVoiceStatus - () => portal voice status (STT tier + corrections)
     * @param {Function} opts.onState - (state) => render 'idle'|'recording'|'processing'
     * @param {Function} opts.onResult - (text) => non-empty trimmed transcript
     * @param {Function} opts.onError - (kind, message) => kind is one of
     *   'unsupported' | 'stt' | 'mic' | 'empty' | 'transcribe'
     */
    constructor({ getVoiceStatus, onState, onResult, onError }) {
        this.getVoiceStatus = getVoiceStatus;
        this.onState = onState || (() => {});
        this.onResult = onResult || (() => {});
        this.onError = onError || (() => {});
        this.state = 'idle'; // idle | recording | processing
        this._mediaRecorder = null;
        this._audioChunks = [];
        this._sttCancelled = false;
    }

    usesBrowserStt() {
        // Server-side tiers (cloud, custom, default-with-Moonshine) upload
        // audio to /transcribe; otherwise recognition happens in the browser.
        return !browserStt.serverTranscribes(this.getVoiceStatus());
    }

    _setState(state) {
        this.state = state;
        this.onState(state);
    }

    async start() {
        if (this.state !== 'idle') return;

        if (this.usesBrowserStt()) {
            if (!browserStt.isSupported()) {
                this.onError('unsupported', 'Browser voice input requires Chrome (or set stt.backend: cloud/custom)');
                return;
            }
            this._sttCancelled = false;
            const ok = browserStt.start({
                onFinal: (text) => {
                    this._setState('idle');
                    if (this._sttCancelled || !text) return;
                    this.onResult(text);
                },
                onError: (err) => {
                    this._setState('idle');
                    this.onError('stt', `Speech recognition failed: ${err}`);
                },
            }, this.getVoiceStatus()?.corrections || {});
            if (ok) this._setState('recording');
            return;
        }

        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this._audioChunks = [];
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus'
                : (MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '');
            this._mediaRecorder = mimeType
                ? new MediaRecorder(stream, { mimeType })
                : new MediaRecorder(stream);
            this._mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) this._audioChunks.push(e.data);
            };
            this._mediaRecorder.onstop = () => {
                // Release the microphone; only process if we weren't cancelled
                stream.getTracks().forEach((t) => t.stop());
                if (this._audioChunks.length > 0 && this.state === 'processing') {
                    const blob = new Blob(this._audioChunks, {
                        type: this._mediaRecorder.mimeType || 'audio/webm',
                    });
                    this._transcribe(blob);
                }
            };
            this._mediaRecorder.start();
            this._setState('recording');
        } catch (err) {
            console.error('[PTT] Failed to start recording:', err);
            this._setState('idle');
            this.onError('mic', 'Microphone access denied');
        }
    }

    stop() {
        if (this.state !== 'recording') return;
        this._setState('processing');
        if (this.usesBrowserStt()) {
            browserStt.stop(); // onFinal fires from onend
            return;
        }
        this._mediaRecorder?.stop();
    }

    cancel() {
        if (this.state !== 'recording') return;
        this._setState('idle');
        if (this.usesBrowserStt()) {
            this._sttCancelled = true;
            browserStt.stop();
            return;
        }
        this._audioChunks = [];
        this._mediaRecorder?.stop(); // onstop skips processing (state is idle)
    }

    async _transcribe(blob) {
        try {
            const formData = new FormData();
            formData.append('audio', blob, 'recording.webm');
            const res = await apiFetch('/transcribe', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.error) throw new Error(data.error);
            const text = data.text?.trim();
            if (text) this.onResult(text);
            else this.onError('empty', 'No speech detected');
        } catch (err) {
            console.error('[PTT] Transcription failed:', err);
            this.onError('transcribe', err.message || 'Transcription failed');
        } finally {
            this._setState('idle');
        }
    }
}
