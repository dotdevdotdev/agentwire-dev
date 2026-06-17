/**
 * Browser speech recognition wrapper (default STT tier).
 *
 * Chrome SpeechRecognition — recognition happens client-side (Google's
 * speech stack), no model download, no GPU, no audio upload. Chrome is the
 * blessed browser for this tier; Safari is shaky, Firefox lacks the API.
 */

import { applyCorrections } from './jargon.js';

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

export function isSupported() {
    return Boolean(SR);
}

/**
 * Whether the server transcribes (upload audio to /transcribe) vs. the browser
 * (SpeechRecognition). The portal decides per /api/voice-status: true for the
 * cloud/custom tiers, and for the default tier once in-process Moonshine is
 * ready. Until then the default tier stays browser-side.
 * @param {Object} voiceStatus - parsed /api/voice-status payload
 */
export function serverTranscribes(voiceStatus) {
    return Boolean(voiceStatus?.stt?.server_transcribe);
}

let activeRecognition = null;

/**
 * Start push-to-talk recognition.
 * @param {Object} handlers
 * @param {Function} handlers.onFinal - (correctedText) called once after stop()
 * @param {Function} handlers.onInterim - (text) live partial results
 * @param {Function} handlers.onError - (errorCode)
 * @param {Object} corrections - extra jargon corrections (from /api/voice-status)
 */
export function start({ onFinal, onInterim, onError } = {}, corrections = {}) {
    if (!SR || activeRecognition) return false;

    const rec = new SR();
    rec.lang = 'en-US';
    rec.continuous = true;       // keep listening until PTT release
    rec.interimResults = true;

    let finalText = '';
    rec.onresult = (e) => {
        let interim = '';
        for (const result of e.results) {
            if (result.isFinal) finalText += result[0].transcript + ' ';
            else interim += result[0].transcript;
        }
        onInterim?.((finalText + interim).trim());
    };
    rec.onerror = (e) => {
        // 'no-speech' / 'aborted' are normal PTT outcomes, not errors
        if (e.error !== 'no-speech' && e.error !== 'aborted') {
            console.error('[BrowserSTT] error:', e.error);
            onError?.(e.error);
        }
    };
    rec.onend = () => {
        activeRecognition = null;
        const text = applyCorrections(finalText.trim(), corrections);
        onFinal?.(text);
    };

    rec.start();
    activeRecognition = rec;
    return true;
}

/** Stop recognition; onFinal fires from onend with the corrected transcript. */
export function stop() {
    activeRecognition?.stop();
}
