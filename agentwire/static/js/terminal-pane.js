/**
 * terminal-pane.js
 *
 * TerminalPane — the terminal CORE extracted from SessionWindow (#763): xterm.js
 * Terminal + FitAddon, the WS connect/attach/resize/reconnect logic, and the
 * mouse-wheel/copy-mode (touch two-finger scroll) handling. Mountable into ANY
 * container — SessionWindow's full terminal window and the Session Workspace
 * card's mini-terminal both wrap the same `new TerminalPane(container, opts)`.
 *
 * Builds its own DOM (`.session-terminal` + `.session-disconnect-overlay` +
 * `.session-status-bar`) directly inside the given container, adding the
 * `session-window-content` class so the existing flex-column/status-bar CSS
 * applies regardless of host — the container just needs a definite size.
 *
 * Owns the WS lifecycle end to end: connect, silent auto-reconnect with
 * backoff, the manual "Reconnect" overlay + any-key-to-reconnect (scoped to
 * this pane's own DOM via a capture-phase listener, so multiple panes never
 * cross-trigger each other), and audio/speak_text passthrough. Session-ended
 * and activity are surfaced to the host via callbacks rather than handled
 * here, since what to DO about them (close a window vs. collapse a card) is
 * host-specific.
 */

import { apiFetch, wsProtocols } from './api.js';
import { desktop } from './desktop-manager.js';
import { getTerminalFontSize, FONT_SIZE_EVENT } from './terminal-font-prefs.js';
import { buildSessionId, normalizeMachine, sameMachine } from './session-id.js';

const NARROW_VIEWPORT = '(max-width: 768px)';
const TERMINAL_FONT_FAMILY = '"FiraMono Nerd Font Mono", Menlo, Monaco, "Courier New", monospace';

function pickTerminalFontSize() {
    return getTerminalFontSize();
}

// Terminal WS reconnect tuning — mirrors the dashboard WS backoff in
// desktop-manager.js. A transient drop should heal silently rather than dump
// the user onto the manual "Reconnect" wall; the tmux session almost always
// outlives the WS.
const TERM_RECONNECT_INITIAL = 500;     // ms before first silent retry
const TERM_RECONNECT_MAX = 10000;       // ms backoff ceiling
const TERM_RECONNECT_MULTIPLIER = 1.6;
const TERM_RECONNECT_OVERLAY_AFTER = 4; // show the manual wall only after N silent retries fail

export class TerminalPane {
    /**
     * @param {HTMLElement} container - Mount point. Must have a definite size;
     *   TerminalPane appends `session-window-content` and builds its DOM inside it.
     * @param {Object} opts
     * @param {string} opts.session - Session name
     * @param {string|null} [opts.machine] - Remote machine ID
     * @param {Function} [opts.onActivity] - Fired when terminal data is received
     * @param {Function} [opts.onSessionEnded] - Fired when the tmux session truly
     *   ended (clean exit message) or a remote session no longer exists on manual
     *   reconnect. The host decides what to do (close a window, collapse a card).
     */
    constructor(container, opts = {}) {
        this.container = container;
        this.session = opts.session;
        this.machine = normalizeMachine(opts.machine);
        // Per-instance font override: the card mini-terminal renders a smaller
        // fixed size than the global terminal pref so more rows fit. null →
        // follow the global pref + responsive default like the full window.
        this._fontSizeOverride = opts.fontSize ?? null;
        this.onActivity = opts.onActivity || (() => {});
        this.onSessionEnded = opts.onSessionEnded || (() => {});

        this.terminal = null;
        this.fitAddon = null;
        this.webglAddon = null;
        this.ws = null;
        this.resizeObserver = null;
        this._inputBound = false;

        this._autoReconnectAttempts = 0;
        this._autoReconnectTimer = null;
        this._destroyed = false;
        this._sessionEnded = false;
        this._overlayKeyHandler = null;

        this._narrowMedia = null;
        this._narrowMediaHandler = null;
        this._fontPrefHandler = null;
        this._touchScrollCleanup = null;

        this._buildDom();
        this._createTerminal();
        this._setupResizeObserver();
        this._setupReconnectButton();
        this._connectWebSocket();
    }

    get sessionId() {
        return buildSessionId(this.session, this.machine);
    }

    focus() {
        this.terminal?.focus();
    }

    /**
     * @param {Object} [opts]
     * @param {boolean} [opts.afterAnimation=false] - Wait for a CSS width/height
     *   transition on `opts.watchEl` to settle before fitting (e.g. a WinBox
     *   maximize animation), falling back to a 500ms timer if it never fires.
     * @param {HTMLElement} [opts.watchEl] - Element to watch for transitionend.
     *   Required when afterAnimation is true (the host knows which element, if
     *   any, is actually animating — e.g. the WinBox window, not this pane's
     *   own container).
     */
    fit({ afterAnimation = false, watchEl = null } = {}) {
        if (afterAnimation && watchEl) {
            this._fitAfterAnimation(watchEl);
        } else {
            this._handleResize();
        }
    }

    /** Reconnect if the WS isn't currently open — e.g. on window restore, when
     * the socket may have dropped while minimized. */
    reconnectIfNeeded() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this._connectWebSocket();
        }
    }

    /** Push a status message into this pane's own status bar (e.g. a PTT
     * "Sent: ..." flash). The pane manages its own connect/error states
     * internally; this is for host-driven, transient notices. */
    setStatus(state, message) {
        this._updateStatus(state, message);
    }

    dispose() {
        this._destroyed = true;
        if (this._autoReconnectTimer) {
            clearTimeout(this._autoReconnectTimer);
            this._autoReconnectTimer = null;
        }
        if (this._overlayKeyHandler) {
            this._root.removeEventListener('keydown', this._overlayKeyHandler, true);
            this._overlayKeyHandler = null;
        }
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }
        if (this._narrowMedia && this._narrowMediaHandler) {
            this._narrowMedia.removeEventListener('change', this._narrowMediaHandler);
            this._narrowMedia = null;
            this._narrowMediaHandler = null;
        }
        if (this._fontPrefHandler) {
            window.removeEventListener(FONT_SIZE_EVENT, this._fontPrefHandler);
            this._fontPrefHandler = null;
        }
        if (this._touchScrollCleanup) {
            this._touchScrollCleanup();
            this._touchScrollCleanup = null;
        }
        if (this.ws) {
            // Belt-and-suspenders: an onmessage task already queued before
            // close() can still fire once more (the browser doesn't retract
            // an already-scheduled event dispatch) — null the handler too so
            // it can't reach a torn-down terminal even in that window.
            this.ws.onclose = null;
            this.ws.onmessage = null;
            this.ws.close();
            this.ws = null;
        }
        if (this.terminal) {
            // A just-processed write can leave xterm.js with its OWN internal
            // render/write-buffer flush scheduled for a later animation frame,
            // independent of anything here. Disposing synchronously can tear
            // the renderer down before that frame runs, and xterm's internal
            // code then throws (uncatchable from here — different task).
            // Null the reference now so every guard in this class stops
            // touching it immediately, but defer the actual dispose() one
            // frame so xterm's own pending internal work runs first, against
            // a still-valid instance, before its DOM disappears underneath it.
            const term = this.terminal;
            this.terminal = null;
            requestAnimationFrame(() => {
                try { term.dispose(); } catch (e) {}
            });
        }
        this.fitAddon = null;
        this._root = null;
    }

    // Private methods

    _buildDom() {
        this._root = this.container;
        this._root.classList.add('session-window-content');
        this._root.innerHTML = `
            <div class="session-terminal"></div>
            <div class="session-disconnect-overlay hidden">
                <div class="disconnect-content">
                    <div class="disconnect-message">Session Disconnected</div>
                    <button class="btn btn-primary reconnect-btn">Reconnect</button>
                    <div class="disconnect-hint">or press any key</div>
                </div>
            </div>
            <div class="session-status-bar">
                <span class="status-indicator connecting"></span>
                <span class="status-text">Connecting...</span>
            </div>
        `;
        this._terminalEl = this._root.querySelector('.session-terminal');
    }

    _createTerminal() {
        const terminalEl = this._terminalEl;
        const initialFontSize = this._fontSizeOverride ?? pickTerminalFontSize();
        terminalEl.style.setProperty('--terminal-font-size', `${initialFontSize}px`);

        this.terminal = new Terminal({
            cursorBlink: true,
            fontSize: initialFontSize,
            fontFamily: TERMINAL_FONT_FAMILY,
            altClickMovesCursor: false,
            macOptionClickForcesSelection: true,  // Allow Option/Alt+drag for native selection (bypasses tmux mouse mode)
            theme: {
                background: '#000',
                foreground: '#e6edf3',
                cursor: '#2ea043',
                selection: 'rgba(46, 160, 67, 0.3)',
            },
        });

        this.fitAddon = new FitAddon.FitAddon();
        this.terminal.loadAddon(this.fitAddon);

        // Add WebGL addon for performance (optional). Keep a reference: after a
        // large container resize (tile grid→max) the WebGL renderer can leave
        // the newly-exposed area transparent until its texture atlas is rebuilt.
        this.webglAddon = null;
        try {
            if (typeof WebglAddon !== 'undefined') {
                this.webglAddon = new WebglAddon.WebglAddon();
                this.terminal.loadAddon(this.webglAddon);
            }
        } catch (e) {
            console.warn('[TerminalPane] WebGL not available:', e);
        }

        this.terminal.open(terminalEl);

        // xterm selections are canvas/WebGL-rendered, not DOM selections, so
        // the scratch pad's selectionchange-based popover can't see them.
        // Surface them via a custom event on mouseup (where the pointer is).
        terminalEl.addEventListener('mouseup', (e) => {
            const text = this.terminal?.getSelection();
            if (text && text.trim()) {
                window.dispatchEvent(new CustomEvent('terminal-selection', {
                    detail: { text, x: e.clientX, y: e.clientY, session: this.session },
                }));
            }
        });

        // Touch devices emit no `wheel` events, so xterm's wheel-driven
        // scrolling (tmux copy-mode / the app's own scroll) is unreachable on a
        // tablet. Translate a two-finger vertical pan into synthetic wheel
        // events so touch scrolls history exactly like a desktop mouse wheel.
        // One finger stays free for tap/selection.
        this._setupTouchScroll(terminalEl);

        // Re-pick font size on viewport breakpoint changes (mobile rotation, window resize)
        // and on user override via the sidebar Config slider.
        const applyNewSize = () => {
            if (!this.terminal) return;
            const newSize = this._fontSizeOverride ?? pickTerminalFontSize();
            terminalEl.style.setProperty('--terminal-font-size', `${newSize}px`);
            this.terminal.options.fontSize = newSize;
            this._handleResize();
        };
        this._narrowMedia = window.matchMedia(NARROW_VIEWPORT);
        this._narrowMediaHandler = applyNewSize;
        this._fontPrefHandler = applyNewSize;
        this._narrowMedia.addEventListener('change', this._narrowMediaHandler);
        window.addEventListener(FONT_SIZE_EVENT, this._fontPrefHandler);

        const doInitialFit = (fontLoaded) => {
            requestAnimationFrame(() => {
                // dispose() may have run while waiting on font load/this frame.
                if (!this.terminal) return;
                if (fontLoaded) {
                    // Force xterm to recalculate cell dimensions by re-setting font
                    // This triggers internal re-measurement with the now-loaded font
                    this.terminal.options.fontFamily = TERMINAL_FONT_FAMILY;
                    this.terminal.options.fontSize = initialFontSize;
                }
                this._handleResize();
                setTimeout(() => this._handleResize(), 100);
            });
        };

        if (document.fonts && document.fonts.load) {
            // Wait for font to load, then fit
            document.fonts.load(`${initialFontSize}px ${TERMINAL_FONT_FAMILY}`).then(() => {
                doInitialFit(true);
            }).catch(() => {
                // Font load failed, fit anyway with fallback font
                doInitialFit(false);
            });
        } else {
            // Font loading API not available, use delayed fit
            doInitialFit(false);
        }
    }

    /**
     * Translate a two-finger vertical pan into tmux mouse-wheel scroll.
     *
     * Touch devices emit no `wheel` events, so the desktop scroll path (mouse
     * wheel → xterm encodes an SGR mouse event → tmux enters copy-mode and
     * scrolls) never fires on a tablet. Rather than fake a WheelEvent and hope
     * xterm's mouse encoder picks it up, we send the exact bytes a real wheel
     * produces — the SGR-1006 mouse sequence — straight down the input
     * WebSocket. tmux runs with `mouse on`, so it consumes these and scrolls
     * history. One finger is left untouched for tap/selection.
     *
     * SGR wheel: ESC [ < Btn ; Col ; Row M  — Btn 64 = wheel-up, 65 = wheel-down
     * (1-based Col/Row; any point inside the pane works for scroll).
     */
    _setupTouchScroll(terminalEl) {
        // Finger travel, in text lines, that advances one tmux wheel tick. tmux
        // scrolls 5 lines per wheel tick (its WheelUp/DownPane `-N 5` binding),
        // so a strict 1:1 mapping would need 5 lines of finger travel per tick —
        // on a short window that's nearly the whole draggable height, so you get
        // one tick then nothing. Firing a tick every ~1.5 lines keeps it ticking
        // continuously across the whole stroke; momentum then covers distance.
        const FINGER_LINES_PER_TICK = 1.5;
        // Cap ticks emitted per animation frame so a fast flick can't flood tmux
        // faster than it can redraw (the source of the laggy/stuttery feel).
        const MAX_TICKS_PER_FRAME = 8;
        // First tick of a gesture fires after only this fraction of a full tick
        // of travel, so the start feels immediate instead of dead until ~5 lines.
        const FIRST_TICK_FRACTION = 0.35;
        // Momentum: after lift, keep scrolling and decay velocity each frame.
        const FRICTION = 0.94;          // per-frame velocity multiplier
        const FLING_MIN_V = 0.04;       // px/ms at release needed to start a fling
        const MOMENTUM_STOP_V = 0.012;  // px/ms below which momentum ends

        let active = false;
        let lastMidY = 0;
        let accum = 0;        // unconsumed finger travel (px), sign = direction
        let rafId = null;
        let emitted = false;  // has any tick fired this gesture? (first-tick boost)
        let velocity = 0;     // px/ms, smoothed — drives momentum
        let lastMoveT = 0;
        let lastFrameT = 0;
        let momentum = false;

        const midY = (touches) => (touches[0].clientY + touches[1].clientY) / 2;

        // Finger pixels per tick = lines-per-tick × measured cell height.
        const pxPerTick = () => {
            const rows = this.terminal?.rows || 24;
            const h = terminalEl.getBoundingClientRect().height;
            const cell = rows > 0 && h > 0 ? h / rows : 18;
            return FINGER_LINES_PER_TICK * cell;
        };

        // dir < 0 → wheel-up (older history); dir > 0 → wheel-down (newer).
        const wheelSeq = (dir) => {
            const col = Math.max(1, Math.floor(this.terminal.cols / 2));
            const row = Math.max(1, Math.floor(this.terminal.rows / 2));
            return `\x1b[<${dir < 0 ? 64 : 65};${col};${row}M`;
        };

        const sendTicks = (ticks) => {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.terminal) return;
            this.ws.send(JSON.stringify({ type: 'input', data: wheelSeq(ticks).repeat(Math.abs(ticks)) }));
        };

        // One frame: advance momentum, then batch all due ticks into one message.
        const flush = (now) => {
            rafId = null;

            if (momentum) {
                const dt = Math.min(now - lastFrameT, 50);
                lastFrameT = now;
                accum += velocity * dt;
                velocity *= FRICTION;
                if (Math.abs(velocity) < MOMENTUM_STOP_V) momentum = false;
            }
            if (active || momentum) rafId = requestAnimationFrame(flush);

            const step = pxPerTick();
            // Snappier first tick: lower the threshold until the gesture moves.
            const threshold = emitted ? step : step * FIRST_TICK_FRACTION;
            let ticks = Math.trunc(accum / threshold);
            if (ticks === 0) return;
            if (ticks > MAX_TICKS_PER_FRAME) ticks = MAX_TICKS_PER_FRAME;
            else if (ticks < -MAX_TICKS_PER_FRAME) ticks = -MAX_TICKS_PER_FRAME;
            accum -= ticks * threshold;
            emitted = true;
            sendTicks(ticks);
        };

        const startRaf = () => { if (rafId === null) { lastFrameT = performance.now(); rafId = requestAnimationFrame(flush); } };
        const stopRaf = () => { if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; } };

        const onTouchStart = (e) => {
            if (e.touches.length !== 2) { active = false; momentum = false; stopRaf(); return; }
            active = true;
            momentum = false;
            emitted = false;
            velocity = 0;
            accum = 0;
            lastMidY = midY(e.touches);
            lastMoveT = performance.now();
            e.preventDefault();  // stop the page/WinBox from claiming the gesture
            startRaf();
        };

        const onTouchMove = (e) => {
            if (!active || e.touches.length !== 2) return;
            e.preventDefault();
            const now = performance.now();
            const y = midY(e.touches);
            // Fingers up (y decreases) → newer/down; fingers down → older/up.
            const dy = lastMidY - y;
            accum += dy;
            const dt = now - lastMoveT;
            if (dt > 0) velocity = 0.6 * velocity + 0.4 * (dy / dt);  // smoothed px/ms
            lastMidY = y;
            lastMoveT = now;
        };

        const onTouchEnd = (e) => {
            if (e.touches.length >= 2) return;
            active = false;
            // Carry a fast lift into a decaying fling; otherwise stop clean.
            if (Math.abs(velocity) >= FLING_MIN_V) {
                momentum = true;
                lastFrameT = performance.now();
                startRaf();
            } else {
                velocity = 0;
                accum = 0;
            }
        };

        terminalEl.addEventListener('touchstart', onTouchStart, { passive: false });
        terminalEl.addEventListener('touchmove', onTouchMove, { passive: false });
        terminalEl.addEventListener('touchend', onTouchEnd);
        terminalEl.addEventListener('touchcancel', onTouchEnd);

        this._touchScrollCleanup = () => {
            stopRaf();
            terminalEl.removeEventListener('touchstart', onTouchStart);
            terminalEl.removeEventListener('touchmove', onTouchMove);
            terminalEl.removeEventListener('touchend', onTouchEnd);
            terminalEl.removeEventListener('touchcancel', onTouchEnd);
        };
    }

    _connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionPath = this.sessionId;

        // Force layout reflow so fitAddon.fit() gets real container dimensions,
        // then pass cols/rows as query params so the server creates the PTY at
        // the correct size from the start (avoids dots on first render).
        if (this.fitAddon && this.terminal) {
            try { this.fitAddon.fit(); } catch (e) {}
        }
        const cols = this.terminal ? this.terminal.cols : 80;
        const rows = this.terminal ? this.terminal.rows : 24;
        const endpoint = `/ws/terminal/${sessionPath}?cols=${cols}&rows=${rows}`;
        const url = `${protocol}//${location.host}${endpoint}`;

        // Close any existing WS (even if still CONNECTING) to avoid orphaned
        // attaches that would receive duplicate broadcast output from tmux.
        if (this.ws) {
            try { this.ws.onclose = null; this.ws.close(); } catch (e) {}
            this.ws = null;
        }

        this.ws = new WebSocket(url, wsProtocols());
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            this._updateStatus('connected', 'Connected');
            this._hideDisconnectOverlay();

            // Healed — clear any pending silent-retry state.
            this._autoReconnectAttempts = 0;
            if (this._autoReconnectTimer) {
                clearTimeout(this._autoReconnectTimer);
                this._autoReconnectTimer = null;
            }

            // Re-fit terminal before sending size — the maximize animation may have
            // completed while the socket was connecting, so fit now to get current dims
            if (this.fitAddon && this.terminal) {
                this.fitAddon.fit();
            }
            this._sendResize();
        };

        this.ws.onmessage = (event) => {
            const data = event.data;

            // Check if this looks like a JSON message from the server
            if (typeof data === 'string' && data.includes('"type"')) {
                try {
                    const msg = JSON.parse(data);

                    if (msg.type === 'audio' && msg.data) {
                        desktop._playAudio(msg.data, this.sessionId);
                        return;
                    } else if (msg.type === 'speak_text' && msg.text) {
                        desktop._speakText(msg.text, this.sessionId);
                        return;
                    } else if (msg.type === 'tts_start') {
                        return;
                    } else if (msg.type === 'session_unlocked' || msg.type === 'session_locked') {
                        return; // Ignore lock messages
                    } else if (msg.type === 'remote_session_ended' || msg.type === 'local_session_ended') {
                        // Clean exit - tmux session truly ended
                        this._sessionEnded = true;
                        this.onSessionEnded();
                        return;
                    } else if (msg.type === 'remote_disconnected' || msg.type === 'local_disconnected') {
                        // Transient drop (bg process side effect, portal restart, etc) -
                        // retry silently with backoff instead of dropping the user onto
                        // the manual wall. The onclose that follows is deduped by the
                        // scheduler's timer guard.
                        this._scheduleAutoReconnect();
                        return;
                    }
                    // Other JSON messages - don't write to terminal
                    return;
                } catch (e) {
                    // Fall through to terminal
                }
            }

            if (!this.terminal) return;
            if (data instanceof ArrayBuffer) {
                this.terminal.write(new Uint8Array(data));
            } else {
                this.terminal.write(data);
            }
            this.onActivity();
        };

        this.ws.onerror = (error) => {
            console.error('[TerminalPane] WebSocket error:', error);
            this._updateStatus('error', 'Connection error');
        };

        this.ws.onclose = () => {
            // The session truly ended (host already tearing down) or we're
            // disposing — nothing to recover.
            if (this._sessionEnded || this._destroyed) return;

            // Any other close — clean (1000) or abrupt — is treated as a transient drop:
            // a bg-process kill, portal hot-reload, or network blip. Retry silently with
            // backoff rather than destroying the terminal or throwing up the manual
            // wall; the overlay only appears once several silent retries have failed.
            this._scheduleAutoReconnect();
        };

        // Only attach once — xterm.js stacks onData listeners, so re-attaching
        // on every _connectWebSocket() (initial + reconnects) would multiply
        // each keystroke.
        if (this.terminal && !this._inputBound) {
            this._inputBound = true;
            this.terminal.onData((data) => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'input', data }));
                }
            });
        }
    }

    _setupResizeObserver() {
        if (!this._terminalEl) return;
        this.resizeObserver = new ResizeObserver(() => this._handleResize());
        this.resizeObserver.observe(this._terminalEl);
    }

    _handleResize() {
        if (!this.fitAddon || !this.terminal) return;
        requestAnimationFrame(() => {
            // dispose() may have run between scheduling and this frame (e.g. a
            // card collapsed while its resize was in flight) — re-check rather
            // than trusting the guard taken before the async hop.
            if (!this.fitAddon || !this.terminal) return;
            try {
                this.terminal.options.fontFamily = TERMINAL_FONT_FAMILY;
                this.terminal.options.fontSize = this._fontSizeOverride ?? pickTerminalFontSize();
                this.fitAddon.fit();
                this._sendResize();
            } catch (e) {
                console.error('[TerminalPane] resize error:', e);
            }
        });
    }

    /**
     * Force a full WebGL repaint. fit() resizes the buffer but doesn't redraw, so
     * after a large container resize (tile grid → maximized) the newly-exposed
     * canvas can stay transparent until interaction. The WebGL renderer needs its
     * stale texture atlas rebuilt; then a full viewport refresh repaints every cell.
     */
    _forceRepaint() {
        try { this.webglAddon?.clearTextureAtlas(); } catch (e) {}
        this.terminal.refresh(0, this.terminal.rows - 1);
    }

    _fitAfterAnimation(watchEl) {
        if (!this.fitAddon || !this.terminal) return;

        const doFit = () => {
            // dispose() may have run while waiting on the transition/timeout.
            if (!this.fitAddon || !this.terminal) return;
            try {
                this.terminal.options.fontFamily = TERMINAL_FONT_FAMILY;
                this.terminal.options.fontSize = this._fontSizeOverride ?? pickTerminalFontSize();
                this.fitAddon.fit();
                this._sendResize();
                this._forceRepaint();
            } catch (err) {
                console.error('[TerminalPane] Fit error:', err);
            }
        };

        let handled = false;
        const onTransitionEnd = (e) => {
            if (e.target === watchEl && (e.propertyName === 'width' || e.propertyName === 'height')) {
                handled = true;
                watchEl.removeEventListener('transitionend', onTransitionEnd);
                doFit();
            }
        };
        watchEl.addEventListener('transitionend', onTransitionEnd);

        // Fallback: if transitionend doesn't fire within 500ms, force fit
        setTimeout(() => {
            if (!handled) {
                watchEl.removeEventListener('transitionend', onTransitionEnd);
                doFit();
            }
        }, 500);
    }

    _sendResize() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN && this.terminal) {
            this.ws.send(JSON.stringify({ type: 'resize', cols: this.terminal.cols, rows: this.terminal.rows }));
        }
    }

    _updateStatus(state, message) {
        if (!this._root) return;
        const statusBar = this._root.querySelector('.session-status-bar');
        if (!statusBar) return;
        const indicator = statusBar.querySelector('.status-indicator');
        const text = statusBar.querySelector('.status-text');
        if (indicator) indicator.className = `status-indicator ${state}`;
        if (text) text.textContent = message;
    }

    _showDisconnectOverlay() {
        const overlay = this._root?.querySelector('.session-disconnect-overlay');
        if (overlay) overlay.classList.remove('hidden');
    }

    _hideDisconnectOverlay() {
        const overlay = this._root?.querySelector('.session-disconnect-overlay');
        if (overlay) overlay.classList.add('hidden');
    }

    /**
     * Schedule a silent reconnect with exponential backoff. The terminal heals
     * itself when the WS comes back (portal restart, transient kill side-effect)
     * instead of forcing a manual click. The manual "Reconnect" overlay surfaces
     * only after TERM_RECONNECT_OVERLAY_AFTER silent attempts have failed, so a
     * brief blip never throws up the wall.
     */
    _scheduleAutoReconnect() {
        if (this._destroyed || this._sessionEnded) return;
        if (this._autoReconnectTimer) return; // already pending — dedupe

        const delay = Math.min(
            TERM_RECONNECT_INITIAL * Math.pow(TERM_RECONNECT_MULTIPLIER, this._autoReconnectAttempts),
            TERM_RECONNECT_MAX
        );
        this._autoReconnectAttempts++;

        if (this._autoReconnectAttempts > TERM_RECONNECT_OVERLAY_AFTER) {
            this._updateStatus('disconnected', 'Connection lost');
            this._showDisconnectOverlay();
        } else {
            this._updateStatus('connecting', 'Reconnecting…');
        }

        this._autoReconnectTimer = setTimeout(() => {
            this._autoReconnectTimer = null;
            if (this._destroyed || this._sessionEnded) return;
            this._connectWebSocket();
        }, delay);
    }

    async _reconnect() {
        // Manual reconnect (button or any-keystroke) — cancel any pending silent
        // retry and reset backoff so this fires immediately.
        if (this._autoReconnectTimer) {
            clearTimeout(this._autoReconnectTimer);
            this._autoReconnectTimer = null;
        }
        this._autoReconnectAttempts = 0;

        this._updateStatus('connecting', 'Checking session...');

        // For remote sessions, check if the session still exists before reconnecting
        if (this.machine) {
            try {
                const response = await apiFetch(`/api/sessions/remote`);
                const data = await response.json();
                const allSessions = (data.machines || []).flatMap(m => m.sessions || []);
                const sessionExists = allSessions.some(s =>
                    s.name === this.session && sameMachine(s.machine, this.machine)
                );

                if (!sessionExists) {
                    this._sessionEnded = true;
                    this.onSessionEnded();
                    return;
                }
            } catch (err) {
                console.error('[TerminalPane] Failed to check session:', err);
                // Continue with reconnect attempt anyway
            }
        }

        this._hideDisconnectOverlay();
        this._updateStatus('connecting', 'Reconnecting...');

        if (this.ws) {
            this.ws.onclose = null; // Prevent triggering overlay again
            this.ws.close();
            this.ws = null;
        }

        this.terminal?.clear();
        this._connectWebSocket();
    }

    _setupReconnectButton() {
        const reconnectBtn = this._root.querySelector('.reconnect-btn');
        if (reconnectBtn) {
            reconnectBtn.addEventListener('click', () => this._reconnect());
        }

        // Any-keystroke fallback: while the disconnect overlay is up, any key
        // reconnects — no need to aim for the small button. Capture phase on
        // this pane's own root so xterm.js (still focused under the overlay)
        // can't swallow the keydown, and so it's inherently scoped to THIS
        // pane — a keypress in another pane's terminal never reaches here.
        this._overlayKeyHandler = (e) => {
            const overlay = this._root.querySelector('.session-disconnect-overlay');
            if (!overlay || overlay.classList.contains('hidden')) return;
            // Bare modifier presses (Shift/Ctrl/Alt/Meta) and OS shortcuts like
            // Cmd-Tab shouldn't count as the "any key".
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
            e.preventDefault();
            e.stopPropagation();
            this._reconnect();
        };
        this._root.addEventListener('keydown', this._overlayKeyHandler, true);
    }
}
