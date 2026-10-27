/**
 * audio-unlock.js — the ONE shared AudioContext + Safari/iOS gesture unlock.
 *
 * Safari (desktop and iOS) leaves an AudioContext suspended unless it is
 * created/resumed inside a user gesture it accepts: pointerdown, touchend or
 * click. keydown alone does NOT count (so the Ctrl+Space PTT chord never
 * unlocks anything). Creating the context lazily at playback time — with no
 * gesture on the stack — therefore plays silence. Chrome is lenient, which
 * hides the bug.
 *
 * Contract:
 *   - installGestureUnlock() once at page start: the first accepted gesture
 *     creates/resumes the shared context and plays a one-sample silent buffer
 *     (the full unlock). It keeps listening so a later suspension (Safari
 *     "interrupted" state) is re-unlocked by the next click.
 *   - requestPlayback(onUnlock) at playback time: returns the context when it
 *     is running, otherwise null and arranges for onUnlock to fire (once) when
 *     a gesture unlocks it — callers queue and replay instead of dropping.
 */

const GESTURES = ['pointerdown', 'touchend', 'click'];

let ctx = null;
let installed = false;
const waiters = new Set();

export function getAudioContext() {
    if (!ctx) {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return ctx;
}

export function isAudioRunning() {
    return !!ctx && ctx.state === 'running';
}

function flushWaiters() {
    const pending = [...waiters];
    waiters.clear();
    for (const cb of pending) cb();
}

/** Create/resume the shared context and play a silent sample. Call from a gesture. */
export async function unlockAudio() {
    const c = getAudioContext();
    try {
        const resumed = c.state === 'suspended' ? c.resume() : Promise.resolve();
        const silent = c.createBuffer(1, 1, 22050);
        const src = c.createBufferSource();
        src.buffer = silent;
        src.connect(c.destination);
        src.start(0);
        await resumed;
    } catch (err) {
        console.warn('[audio-unlock] unlock failed:', err);
    }
    if (isAudioRunning()) flushWaiters();
    return isAudioRunning();
}

export function installGestureUnlock() {
    if (installed) return;
    installed = true;
    const onGesture = () => {
        if (isAudioRunning()) return;
        unlockAudio();
    };
    for (const type of GESTURES) {
        window.addEventListener(type, onGesture, { capture: true, passive: true });
    }
}

/**
 * @param {() => void} onUnlock - called once when a gesture unlocks audio
 * @returns {AudioContext|null} the running context, or null if still locked
 */
export function requestPlayback(onUnlock) {
    if (isAudioRunning()) return ctx;
    // A previously-unlocked context that merely idles as suspended can resume
    // without a gesture in Chrome; Safari leaves this promise pending, so never await it.
    if (ctx) ctx.resume().then(() => { if (isAudioRunning()) flushWaiters(); }).catch(() => {});
    waiters.add(onUnlock);
    return null;
}
