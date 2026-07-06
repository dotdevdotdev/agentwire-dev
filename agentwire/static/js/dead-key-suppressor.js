/**
 * Dead-key composition suppressor — shared by Alt/Option hotkeys.
 *
 * macOS quirk: Option+<letter> combos are dead keys (Option+` starts a
 * grave-accent composition, Option+N a tilde) against the focused element —
 * usually an xterm <textarea> — before any keydown handler runs, and the
 * composed character is delivered through the composition/input path, which
 * preventDefault on keydown cannot cancel. Result: hotkeys "work" but leak
 * a stray ˜ or ` into the active session.
 *
 * Fix: hotkey handlers call arm() right before acting; composition events are
 * swallowed in the CAPTURE phase on window for a short window. xterm's
 * composition listeners live on the textarea (target phase), so stopping
 * propagation here means the accent never reaches the PTY.
 *
 * Chrome only finalizes a pending dead-key composition on blur or on the
 * next keypress — which can be long after the suppression window closes.
 * arm() therefore force-finalizes by blurring the focused xterm textarea
 * (compositionend fires immediately, inside the window) and restoring focus
 * on the next frame.
 */

let suppressUntil = 0;

for (const type of ['compositionstart', 'compositionupdate', 'compositionend']) {
    window.addEventListener(type, (e) => {
        if (performance.now() > suppressUntil) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        // The browser still commits the composed char into the target's value
        // natively; xterm never saw the composition, so the residue would
        // linger (xterm only rewrites the value through its own composition
        // handling) and skew its position bookkeeping for the next real IME
        // composition. Wipe it.
        if (e.type === 'compositionend' && e.target?.classList?.contains('xterm-helper-textarea')) {
            e.target.value = '';
        }
    }, true);
}

/**
 * Swallow composition events for the next `ms` milliseconds and force any
 * pending dead-key composition on an xterm textarea to finalize (and be
 * swallowed) right now.
 */
export function armDeadKeySuppressor(ms = 700) {
    suppressUntil = performance.now() + ms;
    const ae = document.activeElement;
    if (ae?.classList?.contains('xterm-helper-textarea')) {
        ae.blur();  // forces Chrome to commit the composition into the armed window
        requestAnimationFrame(() => ae.focus());
    }
}
