/**
 * Browser speech synthesis (default TTS tier).
 *
 * Speaks `speak_text` WebSocket messages via speechSynthesis. Robotic but
 * free, offline, zero setup — the instant-mode voice. Sentence-chunked
 * because Chrome silently cancels utterances longer than ~15s.
 */

// Defensive client-side strip — the server already strips inline tags in the
// default tier, but a tag must never be spoken literally no matter the path.
const TAG_RE = /(?:^|(?<=\s))(?:\[[a-z][a-z _-]{0,30}\]|<[a-z]+:[^>]{0,60}>)(?=[\s.,!?;:]|$)/g;

export function stripTags(text) {
    return text.replace(TAG_RE, '').replace(/\s{2,}/g, ' ').trim();
}

function chunkSentences(text, maxLen = 200) {
    const sentences = text.match(/[^.!?]+[.!?]+["']?\s*|[^.!?]+$/g) || [text];
    const chunks = [];
    let current = '';
    for (const s of sentences) {
        if (current && (current + s).length > maxLen) {
            chunks.push(current.trim());
            current = s;
        } else {
            current += s;
        }
    }
    if (current.trim()) chunks.push(current.trim());
    return chunks;
}

let voiceCache = null;
function resolveVoice(preferred) {
    if (!preferred || preferred === 'default') return null;
    if (!voiceCache || voiceCache.length === 0) {
        voiceCache = speechSynthesis.getVoices();
    }
    const needle = preferred.toLowerCase();
    return voiceCache.find(v => v.name.toLowerCase().includes(needle)) || null;
}
// Chrome populates voices asynchronously
if (typeof speechSynthesis !== 'undefined') {
    speechSynthesis.onvoiceschanged = () => { voiceCache = speechSynthesis.getVoices(); };
}

const queue = [];
let speaking = false;

function playNext() {
    if (queue.length === 0) {
        speaking = false;
        return;
    }
    speaking = true;
    const { chunk, voiceName, onEnd, isLast } = queue.shift();
    const utterance = new SpeechSynthesisUtterance(chunk);
    const voice = resolveVoice(voiceName);
    if (voice) utterance.voice = voice;
    utterance.onend = () => {
        if (isLast) onEnd?.();
        playNext();
    };
    utterance.onerror = () => {
        if (isLast) onEnd?.();
        playNext();
    };
    speechSynthesis.speak(utterance);
}

/**
 * Queue text for browser speech.
 * @param {string} text - Text to speak (tags stripped defensively)
 * @param {Object} opts
 * @param {string} opts.voiceName - substring-matched against browser voices
 * @param {Function} opts.onEnd - called when this text finishes speaking
 */
export function speak(text, { voiceName = null, onEnd = null } = {}) {
    const clean = stripTags(text);
    if (!clean) {
        onEnd?.();
        return;
    }
    const chunks = chunkSentences(clean);
    chunks.forEach((chunk, i) => {
        queue.push({ chunk, voiceName, onEnd, isLast: i === chunks.length - 1 });
    });
    if (!speaking) playNext();
}

/** Cancel everything queued and currently speaking. */
export function cancel() {
    queue.length = 0;
    speaking = false;
    speechSynthesis.cancel();
}
