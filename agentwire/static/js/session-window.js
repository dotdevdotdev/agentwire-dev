/**
 * session-window.js
 *
 * SessionWindow class - encapsulates a terminal window for a session.
 * Wraps WinBox window, xterm.js Terminal, and WebSocket connection.
 * Supports two modes: Monitor (read-only) and Terminal (interactive).
 */


import { apiFetch, wsProtocols } from './api.js';
import { desktop } from './desktop-manager.js';
import { sessionIcons } from './icon-manager.js';
import { getTerminalFontSize, FONT_SIZE_EVENT } from './terminal-font-prefs.js';
import { buildSessionId, normalizeMachine, sameMachine } from './session-id.js';
import { ansiToHtml } from './utils/ansi.js';
import * as browserStt from './voice/browser-stt.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';

const NARROW_VIEWPORT = '(max-width: 768px)';
function pickTerminalFontSize() {
    return getTerminalFontSize();
}

// Touch-primary devices (tablets/phones) raise the on-screen keyboard the
// instant xterm's hidden textarea is focused. Opening or switching to a window
// shouldn't do that uninvited — the user taps the terminal to type, which
// focuses xterm and raises the keyboard only when they actually want it. On a
// mouse/trackpad device we still auto-focus so typing works immediately.
const TOUCH_PRIMARY = typeof window !== 'undefined'
    && window.matchMedia
    && window.matchMedia('(pointer: coarse)').matches;

// Terminal WS reconnect tuning. A transient drop (portal restart, an
// over-broad bg-process kill, a network blip) should heal silently rather than
// dump the user onto the manual "Reconnect" wall — the tmux session almost
// always outlives the WS. Mirrors the dashboard WS backoff in desktop-manager.js.
const TERM_RECONNECT_INITIAL = 500;     // ms before first silent retry
const TERM_RECONNECT_MAX = 10000;       // ms backoff ceiling
const TERM_RECONNECT_MULTIPLIER = 1.6;
const TERM_RECONNECT_OVERLAY_AFTER = 4; // show the manual wall only after N silent retries fail

export class SessionWindow {
    /**
     * @param {Object} options
     * @param {string} options.session - Session name
     * @param {'monitor'|'terminal'} options.mode - Window mode
     * @param {string|null} options.machine - Remote machine ID (optional)
     * @param {HTMLElement} options.root - Parent element for WinBox
     * @param {Function} options.onClose - Callback when window closes
     * @param {Function} options.onFocus - Callback when window gains focus
     */
    constructor(options) {
        this.session = options.session;
        this.mode = options.mode || 'terminal';
        this.machine = normalizeMachine(options.machine);
        this.root = options.root || document.body;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;

        this.winbox = null;
        this.terminal = null;
        this.outputEl = null;  // For monitor mode
        this.fitAddon = null;
        this.ws = null;
        this.resizeObserver = null;
        this.isOpen = false;

        // Silent reconnect state for the terminal/monitor WS.
        this._autoReconnectAttempts = 0;
        this._autoReconnectTimer = null;
        this._destroyed = false;       // set in close() so a stray onclose can't re-dial
        this._sessionEnded = false;    // true only when the tmux session truly ended (window closes)
        this._overlayKeyHandler = null;

        // PTT (Push-to-talk) state
        this.pttButton = null;
        this.mediaRecorder = null;
        this.audioChunks = [];
        this.pttState = 'idle'; // idle | recording | processing

        // Activity indicator state
        this.activityIndicator = null;
        this.activityState = 'idle'; // idle | processing | generating | playing
        this._activityHandler = null;
        this._ttsStartHandler = null;
        this._audioHandler = null;
        this._audioEndedHandler = null;
        this._activityTimeout = null;
        this._activityThreshold = 3000; // ms before considered idle
    }

    /**
     * Open the session window.
     * Creates WinBox, initializes terminal, connects WebSocket.
     */
    open() {
        if (this.isOpen) {
            this.focus();
            return;
        }

        const container = this._createContainer();
        // Create WinBox FIRST so container is in DOM with real dimensions
        this._createWinBox(container);
        // Now create terminal - fit addon will have actual dimensions to work with
        this._createTerminal(container);
        // Re-trigger resize after terminal is created — onmaximize fired before terminal
        // existed so the initial fit was a no-op; now fit with real dimensions
        if (this.mode === 'terminal') {
            this._handleResizeAfterAnimation();
        }
        this._connectWebSocket();
        this._setupResizeObserver(container);
        // Set up PTT button for terminal mode
        if (this.mode === 'terminal') {
            this._setupPTT(container);
        }
        // Set up reconnect button handler
        this._setupReconnectButton(container);
        // Set up activity indicator in title bar
        this._setupActivityIndicator();

        this.isOpen = true;

        // Focus the terminal so the user can type immediately. Deferred to the
        // next frame so WinBox's maximize animation has settled — focusing
        // during the transition gets stolen back by the parent. Skipped on
        // touch devices so opening a session doesn't pop the soft keyboard —
        // the user taps the terminal to type.
        if (this.mode === 'terminal' && !TOUCH_PRIMARY) {
            requestAnimationFrame(() => {
                if (this.terminal) this.terminal.focus();
            });
        }
    }

    /**
     * Close the session window and clean up resources.
     */
    close() {
        if (!this.isOpen) return;

        // Stop any silent reconnect from re-dialing a window we're tearing down.
        this._destroyed = true;
        if (this._autoReconnectTimer) {
            clearTimeout(this._autoReconnectTimer);
            this._autoReconnectTimer = null;
        }
        if (this._overlayKeyHandler) {
            document.removeEventListener('keydown', this._overlayKeyHandler, true);
            this._overlayKeyHandler = null;
        }

        // Clean up resize observer
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }

        // Clean up viewport breakpoint listener
        if (this._narrowMedia && this._narrowMediaHandler) {
            this._narrowMedia.removeEventListener('change', this._narrowMediaHandler);
            this._narrowMedia = null;
            this._narrowMediaHandler = null;
        }
        if (this._fontPrefHandler) {
            window.removeEventListener(FONT_SIZE_EVENT, this._fontPrefHandler);
            this._fontPrefHandler = null;
        }

        // Clean up PTT keyboard handler
        if (this._pttKeyHandler) {
            document.removeEventListener('keydown', this._pttKeyHandler);
            document.removeEventListener('keyup', this._pttKeyHandler);
            this._pttKeyHandler = null;
        }

        // Clean up activity indicator event handlers
        if (this._activityHandler) {
            desktop.off('session_activity', this._activityHandler);
            this._activityHandler = null;
        }
        if (this._ttsStartHandler) {
            desktop.off('tts_start', this._ttsStartHandler);
            this._ttsStartHandler = null;
        }
        if (this._audioHandler) {
            desktop.off('audio', this._audioHandler);
            this._audioHandler = null;
        }
        if (this._audioEndedHandler) {
            desktop.off('audio_ended', this._audioEndedHandler);
            this._audioEndedHandler = null;
        }
        if (this._activityTimeout) {
            clearTimeout(this._activityTimeout);
            this._activityTimeout = null;
        }

        // Cancel any active recording
        if (this.mediaRecorder && this.pttState === 'recording') {
            this._cancelRecording();
        }

        // Close WebSocket
        if (this.ws) {
            this.ws.onclose = null; // _destroyed already guards, but don't even fire
            this.ws.close();
            this.ws = null;
        }

        // Remove the two-finger touch-scroll listeners
        if (this._touchScrollCleanup) {
            this._touchScrollCleanup();
            this._touchScrollCleanup = null;
        }

        // Dispose terminal (terminal mode) or output element (monitor mode)
        if (this.terminal) {
            this.terminal.dispose();
            this.terminal = null;
        }
        this.outputEl = null;
        this.fitAddon = null;

        // Close WinBox (if not already closed)
        if (this.winbox) {
            // Prevent recursive close call
            const wb = this.winbox;
            this.winbox = null;
            wb.close();
        }

        // Unregister from desktop manager
        desktop.unregisterWindow(this.sessionId);

        this.isOpen = false;

        // Callback
        if (this.onCloseCallback) {
            this.onCloseCallback(this);
        }
    }

    /**
     * Focus the window.
     */
    focus() {
        if (this.winbox) {
            this.winbox.focus();
        }
        // On touch devices, don't pull focus into the terminal input — that
        // raises the soft keyboard on every window switch. Bring the window
        // forward only; tapping the terminal focuses it (and shows the keyboard)
        // when the user actually wants to type.
        if (this.terminal && !TOUCH_PRIMARY) {
            this.terminal.focus();
        }
    }

    /**
     * Minimize the window.
     */
    minimize() {
        if (this.winbox) {
            this.winbox.minimize();
        }
    }

    /**
     * Restore the window from minimized state.
     */
    restore() {
        if (this.winbox) {
            this.winbox.restore();
        }
    }

    /**
     * Check if window is minimized.
     */
    get isMinimized() {
        return this.winbox ? this.winbox.min : false;
    }

    /**
     * Get the full session identifier (includes machine if remote).
     */
    get sessionId() {
        return buildSessionId(this.session, this.machine);
    }

    // Private methods

    _createContainer() {
        const container = document.createElement('div');
        container.className = 'session-window-content';

        if (this.mode === 'monitor') {
            // Monitor mode: simple pre element for text output
            container.innerHTML = `
                <pre class="session-output"></pre>
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
        } else {
            // Terminal mode: xterm.js for interactive terminal. PTT button lives in the
            // WinBox titlebar (see _setupPTTInTitlebar), not inside the content area.
            container.innerHTML = `
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
        }
        return container;
    }

    _createTerminal(container) {
        if (this.mode === 'monitor') {
            // Monitor mode: just store reference to pre element
            this.outputEl = container.querySelector('.session-output');
            return;
        }

        // Terminal mode: full xterm.js setup
        const terminalEl = container.querySelector('.session-terminal');
        this._terminalEl = terminalEl;

        const initialFontSize = pickTerminalFontSize();
        terminalEl.style.setProperty('--terminal-font-size', `${initialFontSize}px`);

        this.terminal = new Terminal({
            cursorBlink: true,
            fontSize: initialFontSize,
            fontFamily: '"FiraMono Nerd Font Mono", Menlo, Monaco, "Courier New", monospace',
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
            console.warn('[SessionWindow] WebGL not available:', e);
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

        // Fit after font loads and layout is complete
        const fontFamily = '"FiraMono Nerd Font Mono", Menlo, Monaco, "Courier New", monospace';
        const fontSize = pickTerminalFontSize();

        // Re-pick font size on viewport breakpoint changes (mobile rotation, window resize)
        // and on user override via the sidebar Config slider.
        const applyNewSize = () => {
            if (!this.terminal) return;
            const newSize = pickTerminalFontSize();
            this._terminalEl?.style.setProperty('--terminal-font-size', `${newSize}px`);
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
                if (fontLoaded) {
                    // Force xterm to recalculate cell dimensions by re-setting font
                    // This triggers internal re-measurement with the now-loaded font
                    this.terminal.options.fontFamily = fontFamily;
                    this.terminal.options.fontSize = fontSize;
                }
                this._handleResize();
                setTimeout(() => this._handleResize(), 100);
            });
        };

        if (document.fonts && document.fonts.load) {
            // Wait for font to load, then fit
            document.fonts.load(`${fontSize}px ${fontFamily}`).then(() => {
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

    _createWinBox(container) {
        const title = `${this.sessionId} (${this.mode})`;

        this.winbox = new WinBox({
            title: title,
            icon: sessionIcons.getIcon(this.session),
            mount: container,
            root: this.root,
            width: '100%',
            height: '100%',
            minwidth: 400,
            minheight: 300,
            class: ['session-window', 'no-full', 'no-resize', 'no-move'],
            onclose: () => {
                // WinBox is closing, clean up our resources
                // Set winbox to null first to prevent recursive close
                this.winbox = null;
                this.close();
                return false; // Allow WinBox to proceed with close
            },
            onfocus: () => {
                if (this.onFocusCallback) {
                    this.onFocusCallback(this);
                }
            },
            onresize: () => {
                this._handleResize();
            },
            onmaximize: () => {
                // WinBox animates maximize - wait for animation to complete
                this._handleResizeAfterAnimation();
                // Update taskbar tab to active style
                if (this.onFocusCallback) {
                    this.onFocusCallback(this);
                }
            },
            onminimize: () => {
                // Update taskbar tab to minimized style
                desktop.emit('window_minimized', { id: this.sessionId });
            },
            onrestore: () => {
                // Emit restored event so tile manager can re-apply position
                desktop.emit('window_restored', { id: this.sessionId });
                // Restore from minimize animates
                this._handleResizeAfterAnimation();
                // Reconnect if disconnected
                if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                    this._connectWebSocket();
                }
                // Update taskbar tab to active style
                if (this.onFocusCallback) {
                    this.onFocusCallback(this);
                }
            },
        });

        // Always open maximized
        this.winbox.maximize();

        // Register with desktop manager for window management
        desktop.registerWindow(this.sessionId, this.winbox);
    }

    _connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionPath = this.sessionId;

        // Choose endpoint based on mode
        // Terminal mode: /ws/terminal/{session} - bidirectional
        // Monitor mode: /ws/{session} - JSON messages
        let endpoint;
        if (this.mode === 'terminal') {
            // Force layout reflow so fitAddon.fit() gets real container dimensions,
            // then pass cols/rows as query params so the server creates the PTY at
            // the correct size from the start (avoids dots on first render).
            if (this.fitAddon && this.terminal) {
                try { this.fitAddon.fit(); } catch (e) {}
            }
            const cols = this.terminal ? this.terminal.cols : 80;
            const rows = this.terminal ? this.terminal.rows : 24;
            endpoint = `/ws/terminal/${sessionPath}?cols=${cols}&rows=${rows}`;
        } else {
            endpoint = `/ws/${sessionPath}`;
        }

        const url = `${protocol}//${location.host}${endpoint}`;

        // Close any existing WS (even if still CONNECTING) to avoid orphaned
        // attaches that would receive duplicate broadcast output from tmux.
        if (this.ws) {
            try { this.ws.onclose = null; this.ws.close(); } catch (e) {}
            this.ws = null;
        }

        this.ws = new WebSocket(url, wsProtocols());

        if (this.mode === 'terminal') {
            // Binary data for terminal mode
            this.ws.binaryType = 'arraybuffer';
        }

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
            if (this.mode === 'terminal' && this.fitAddon && this.terminal) {
                this.fitAddon.fit();
            }

            // Send initial terminal size (both modes need it for proper display)
            this._sendResize();
        };

        this.ws.onmessage = (event) => {
            if (this.mode === 'terminal') {
                // Terminal mode: binary data or string to xterm
                // But first check for JSON messages (audio, tts_start, etc.)
                const data = event.data;

                // Check if this looks like a JSON message from the server
                if (typeof data === 'string') {
                    // Check for JSON audio/control messages
                    if (data.includes('"type"')) {
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
                                // Clean exit - tmux session truly ended, close window
                                this._sessionEnded = true;
                                this.close();
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
                }

                if (!this.terminal) return;
                if (data instanceof ArrayBuffer) {
                    this.terminal.write(new Uint8Array(data));
                } else {
                    this.terminal.write(data);
                }
                // Mark activity when terminal data received
                this._markActivity();
            } else {
                // Monitor mode: JSON messages to pre element
                if (!this.outputEl) return;
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'audio' && msg.data) {
                        desktop._playAudio(msg.data, this.sessionId);
                    } else if (msg.type === 'speak_text' && msg.text) {
                        desktop._speakText(msg.text, this.sessionId);
                    } else if (msg.type === 'output' && msg.data) {
                        // Convert ANSI to HTML and display
                        this.outputEl.innerHTML = ansiToHtml(msg.data);
                        this.outputEl.scrollTop = this.outputEl.scrollHeight;
                        // Mark activity when output received
                        this._markActivity();
                    }
                } catch (e) {
                    // Fallback: display as plain text
                    this.outputEl.textContent = event.data;
                }
            }
        };

        this.ws.onerror = (error) => {
            console.error(`[SessionWindow] WebSocket error:`, error);
            this._updateStatus('error', 'Connection error');
        };

        this.ws.onclose = (event) => {
            // The session truly ended (window already closing) or we're tearing down —
            // nothing to recover.
            if (this._sessionEnded || this._destroyed) return;

            // Any other close — clean (1000) or abrupt — is treated as a transient drop:
            // a bg-process kill, portal hot-reload, or network blip. Retry silently with
            // backoff rather than destroying the session UI or throwing up the manual
            // wall; the overlay only appears once several silent retries have failed.
            // (Deduped against the *_disconnected branch by the scheduler's timer guard.)
            this._scheduleAutoReconnect();
        };

        // For terminal mode, send input to WebSocket. Only attach once — xterm.js
        // stacks onData listeners, so re-attaching on every _connectWebSocket()
        // (initial + reconnects) would multiply each keystroke.
        if (this.mode === 'terminal' && this.terminal && !this._inputBound) {
            this._inputBound = true;
            this.terminal.onData((data) => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'input', data }));
                }
            });
        }
    }

    _setupResizeObserver(container) {
        const terminalEl = container.querySelector('.session-terminal');
        if (!terminalEl) return;

        // Only observe resize for terminal mode
        if (terminalEl) {
            this.resizeObserver = new ResizeObserver(() => {
                this._handleResize();
            });
            this.resizeObserver.observe(terminalEl);
        }
    }

    _handleResize() {
        if (this.mode === 'terminal' && this.fitAddon && this.terminal) {
            requestAnimationFrame(() => {
                try {
                    // Ensure font options are correct before fitting
                    const fontFamily = '"FiraMono Nerd Font Mono", Menlo, Monaco, "Courier New", monospace';
                    const fontSize = pickTerminalFontSize();
                    this.terminal.options.fontFamily = fontFamily;
                    this.terminal.options.fontSize = fontSize;

                    this.fitAddon.fit();
                    this._sendResize();
                } catch (e) {
                    console.error('[_handleResize] error:', e);
                }
            });
        }
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

    _handleResizeAfterAnimation() {
        // Listen for CSS transition to complete before fitting terminal
        if (this.mode !== 'terminal' || !this.fitAddon || !this.terminal || !this.winbox) return;

        const doFit = () => {
            try {
                // Ensure font options are set before fitting (in case they weren't applied correctly)
                const fontFamily = '"FiraMono Nerd Font Mono", Menlo, Monaco, "Courier New", monospace';
                const fontSize = pickTerminalFontSize();
                this.terminal.options.fontFamily = fontFamily;
                this.terminal.options.fontSize = fontSize;

                this.fitAddon.fit();
                this._sendResize();
                this._forceRepaint();
            } catch (err) {
                console.error('[SessionWindow] Fit error:', err);
            }
        };

        const winboxEl = this.winbox.window;
        let handled = false;

        const onTransitionEnd = (e) => {
            if (e.target === winboxEl && (e.propertyName === 'width' || e.propertyName === 'height')) {
                handled = true;
                winboxEl.removeEventListener('transitionend', onTransitionEnd);
                doFit();
            }
        };

        winboxEl.addEventListener('transitionend', onTransitionEnd);

        // Fallback: if transitionend doesn't fire within 500ms, force fit
        setTimeout(() => {
            if (!handled) {
                winboxEl.removeEventListener('transitionend', onTransitionEnd);
                doFit();
            }
        }, 500);
    }

    _sendResize() {
        // Only terminal mode sends resize (monitor doesn't need it)
        if (this.mode === 'terminal' && this.ws && this.ws.readyState === WebSocket.OPEN && this.terminal) {
            const msg = {
                type: 'resize',
                cols: this.terminal.cols,
                rows: this.terminal.rows,
            };
            this.ws.send(JSON.stringify(msg));
        }
    }

    _updateStatus(state, message) {
        if (!this.winbox) return;

        const container = this.winbox.body;
        if (!container) return;

        const statusBar = container.querySelector('.session-status-bar');
        if (!statusBar) return;

        const indicator = statusBar.querySelector('.status-indicator');
        const text = statusBar.querySelector('.status-text');

        if (indicator) {
            indicator.className = `status-indicator ${state}`;
        }
        if (text) {
            text.textContent = message;
        }
    }

    _showDisconnectOverlay() {
        if (!this.winbox) return;
        const container = this.winbox.body;
        if (!container) return;

        const overlay = container.querySelector('.session-disconnect-overlay');
        if (overlay) {
            overlay.classList.remove('hidden');
        }
    }

    _hideDisconnectOverlay() {
        if (!this.winbox) return;
        const container = this.winbox.body;
        if (!container) return;

        const overlay = container.querySelector('.session-disconnect-overlay');
        if (overlay) {
            overlay.classList.add('hidden');
        }
    }

    /**
     * Schedule a silent reconnect with exponential backoff. The terminal heals
     * itself when the WS comes back (portal restart, transient kill side-effect)
     * instead of forcing a manual click. The manual "Reconnect" overlay surfaces
     * only after TERM_RECONNECT_OVERLAY_AFTER silent attempts have failed, so a
     * brief blip never throws up the wall. Re-entrant calls (e.g. a *_disconnected
     * message immediately followed by onclose) are coalesced by the timer guard.
     */
    _scheduleAutoReconnect() {
        if (this._destroyed || this._sessionEnded) return;
        if (this._autoReconnectTimer) return; // already pending — dedupe

        const delay = Math.min(
            TERM_RECONNECT_INITIAL * Math.pow(TERM_RECONNECT_MULTIPLIER, this._autoReconnectAttempts),
            TERM_RECONNECT_MAX
        );
        this._autoReconnectAttempts++;

        // Keep it quiet for the first few tries; only raise the wall once the drop
        // looks persistent. Background retries continue either way, so it self-heals.
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
                // Flatten sessions from all machines: {machines: [{sessions: [...]}]} -> [...]
                const allSessions = (data.machines || []).flatMap(m => m.sessions || []);
                const sessionExists = allSessions.some(s =>
                    s.name === this.session && sameMachine(s.machine, this.machine)
                );

                if (!sessionExists) {
                    this.close();
                    return;
                }
            } catch (err) {
                console.error('[SessionWindow] Failed to check session:', err);
                // Continue with reconnect attempt anyway
            }
        }

        this._hideDisconnectOverlay();
        this._updateStatus('connecting', 'Reconnecting...');

        // Close existing connection if any
        if (this.ws) {
            this.ws.onclose = null; // Prevent triggering overlay again
            this.ws.close();
            this.ws = null;
        }

        // Clear terminal to avoid escape sequence garbage on reconnect
        if (this.terminal) {
            this.terminal.clear();
        }

        // Reconnect
        this._connectWebSocket();
    }


    _renderMarkdown(text) {
        if (typeof marked !== 'undefined' && marked.parse) {
            try {
                return marked.parse(text, { breaks: true });
            } catch {
                // Fall through to plain text
            }
        }
        return this._escapeHtml(text).replace(/\n/g, '<br>');
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Reconnect button handler

    _setupReconnectButton(container) {
        const reconnectBtn = container.querySelector('.reconnect-btn');
        if (reconnectBtn) {
            reconnectBtn.addEventListener('click', () => this._reconnect());
        }

        // Any-keystroke fallback: while the disconnect overlay is up, any key
        // reconnects — no need to aim for the small button. Capture phase so
        // xterm.js (still focused under the overlay) can't swallow the keydown.
        // Scoped to the focused window so a keypress can't reconnect a background
        // session window the user didn't mean to touch.
        this._overlayKeyHandler = (e) => {
            if (!this.winbox) return;
            const body = this.winbox.body;
            const overlay = body && body.querySelector('.session-disconnect-overlay');
            if (!overlay || overlay.classList.contains('hidden')) return;
            // Bare modifier presses (Shift/Ctrl/Alt/Meta) and OS shortcuts like
            // Cmd-Tab shouldn't count as the "any key".
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
            // Only the focused window responds; ignore if a different winbox is focused.
            const focusedWin = document.activeElement && document.activeElement.closest('.winbox');
            if (focusedWin && focusedWin !== this.winbox.window) return;
            e.preventDefault();
            e.stopPropagation();
            this._reconnect();
        };
        document.addEventListener('keydown', this._overlayKeyHandler, true);
    }

    // PTT (Push-to-talk) Methods

    _setupPTT(container) {
        // PTT now lives in the WinBox titlebar (next to the activity indicator),
        // not inside the container. Create it and prepend to .wb-title.
        if (!this.winbox) return;
        this._pttContainer = container;  // transcript bar mounts here (default tier)
        const titleEl = this.winbox.window.querySelector('.wb-title');
        if (!titleEl) return;

        this.pttButton = document.createElement('button');
        this.pttButton.className = 'wb-title-ptt';
        this.pttButton.title = 'Hold to record voice input';
        this.pttButton.innerHTML = '<span class="ptt-icon">🎤</span>';
        titleEl.insertBefore(this.pttButton, titleEl.firstChild);

        // WinBox attaches capture-phase mousedown on .wb-drag for window dragging,
        // which swallows our mousedown. Use pointer events with capture phase to beat
        // WinBox to the punch, and setPointerCapture so tiny cursor movements within
        // the small 22px button don't fire pointerleave mid-hold.
        const onDown = (e) => {
            e.preventDefault();
            e.stopPropagation();
            this.pttButton.setPointerCapture?.(e.pointerId);
            this._startRecording();
        };
        const onUp = (e) => {
            e.stopPropagation();
            this.pttButton.releasePointerCapture?.(e.pointerId);
            if (this.pttState === 'recording') this._stopRecording();
        };
        const onCancel = (e) => {
            this.pttButton.releasePointerCapture?.(e.pointerId);
            if (this.pttState === 'recording') this._cancelRecording();
        };
        this.pttButton.addEventListener('pointerdown', onDown, true);
        this.pttButton.addEventListener('pointerup', onUp, true);
        this.pttButton.addEventListener('pointercancel', onCancel);

        // Keyboard shortcut: Ctrl+Space to toggle recording (when window focused)
        this._pttKeyHandler = (e) => {
            // Only respond when this window is focused
            if (!this.winbox || !document.activeElement?.closest('.winbox')?.contains(container)) {
                return;
            }

            // Ctrl+Space (or Cmd+Space on Mac) to record
            if (e.code === 'Space' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                e.stopPropagation();

                if (e.type === 'keydown' && this.pttState === 'idle') {
                    this._startRecording();
                } else if (e.type === 'keyup' && this.pttState === 'recording') {
                    this._stopRecording();
                }
            }
        };

        document.addEventListener('keydown', this._pttKeyHandler);
        document.addEventListener('keyup', this._pttKeyHandler);
    }

    _usesBrowserStt() {
        // Server-side tiers (cloud, custom, default-with-Moonshine) upload audio
        // to /transcribe; otherwise recognition happens in the browser.
        return !browserStt.serverTranscribes(desktop.voiceStatus);
    }

    async _startRecording() {
        if (this.pttState !== 'idle') return;

        // Default tier: recognition happens in the browser (Chrome),
        // transcript lands in an edit-before-send bar — no audio upload.
        if (this._usesBrowserStt()) {
            if (!browserStt.isSupported()) {
                this._updateStatus('error', 'Browser voice input requires Chrome (or set stt.backend: cloud/custom)');
                return;
            }
            this._sttCancelled = false;
            const ok = browserStt.start({
                onFinal: (text) => {
                    this._setPTTState('idle');
                    if (this._sttCancelled || !text) return;
                    if (isAutoSend()) this._sendVoiceText(text);
                    else this._showTranscriptBar(text);
                },
                onError: (err) => {
                    this._updateStatus('error', `Speech recognition failed: ${err}`);
                    this._setPTTState('idle');
                },
            }, desktop.voiceStatus?.corrections || {});
            if (ok) this._setPTTState('recording');
            return;
        }

        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this.audioChunks = [];

            // Use webm/opus for efficient transfer
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus'
                : 'audio/webm';

            this.mediaRecorder = new MediaRecorder(stream, { mimeType });

            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) {
                    this.audioChunks.push(e.data);
                }
            };

            this.mediaRecorder.onstop = () => {
                // Stop all tracks to release microphone
                stream.getTracks().forEach(track => track.stop());

                if (this.audioChunks.length > 0 && this.pttState === 'processing') {
                    const blob = new Blob(this.audioChunks, { type: mimeType });
                    this._processRecording(blob);
                }
            };

            this.mediaRecorder.start();
            this._setPTTState('recording');

        } catch (err) {
            console.error('[SessionWindow] Failed to start recording:', err);
            this._updateStatus('error', 'Microphone access denied');
            this._setPTTState('idle');
        }
    }

    _stopRecording() {
        if (this.pttState !== 'recording') return;

        if (this._usesBrowserStt()) {
            this._setPTTState('processing');
            browserStt.stop();  // onFinal fires from onend
            return;
        }

        if (!this.mediaRecorder) return;
        this._setPTTState('processing');
        this.mediaRecorder.stop();
    }

    _cancelRecording() {
        if (this._usesBrowserStt()) {
            this._sttCancelled = true;
            browserStt.stop();
            this._setPTTState('idle');
            return;
        }

        if (!this.mediaRecorder) return;
        this.audioChunks = [];
        this.mediaRecorder.stop();
        this._setPTTState('idle');
    }

    /**
     * Edit-before-send transcript bar (default STT tier). Browser recognition
     * misses jargon occasionally — a glance catches it before it ships.
     * Mounted as the first child of the window content (never WinBox internals).
     */
    _showTranscriptBar(text) {
        this._removeTranscriptBar();
        if (!this._pttContainer) return;

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
            this._removeTranscriptBar();
            if (value) this._sendVoiceText(value);
        };
        bar.querySelector('.wb-transcript-send').addEventListener('click', send);
        bar.querySelector('.wb-transcript-dismiss').addEventListener('click', () => this._removeTranscriptBar());
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); send(); }
            else if (e.key === 'Escape') { e.preventDefault(); this._removeTranscriptBar(); }
            e.stopPropagation();  // don't leak keys to the terminal
        });

        this._pttContainer.insertBefore(bar, this._pttContainer.firstChild);
        this._transcriptBar = bar;
        input.focus();
        input.select();
    }

    _removeTranscriptBar() {
        this._transcriptBar?.remove();
        this._transcriptBar = null;
        // Hand focus back to the terminal so typing resumes naturally
        this.terminal?.focus();
    }

    async _sendVoiceText(text) {
        try {
            const sendRes = await apiFetch(`/send/${this.sessionId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: voicePromptWrap(text) }),
            });
            const sendData = await sendRes.json();
            if (sendData.error) throw new Error(sendData.error);
            this._updateStatus('connected', `Sent: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`);
            setTimeout(() => {
                if (this.pttState === 'idle') this._updateStatus('connected', 'Connected');
            }, 3000);
        } catch (err) {
            console.error('[SessionWindow] Voice send failed:', err);
            this._updateStatus('error', err.message || 'Voice input failed');
        }
    }

    async _processRecording(blob) {
        try {
            // Step 1: Transcribe audio
            const formData = new FormData();
            formData.append('audio', blob, 'recording.webm');

            const transcribeRes = await apiFetch('/transcribe', {
                method: 'POST',
                body: formData,
            });

            const transcribeData = await transcribeRes.json();

            if (transcribeData.error) {
                throw new Error(transcribeData.error);
            }

            const text = transcribeData.text?.trim();
            if (!text) {
                this._updateStatus('error', 'No speech detected');
                this._setPTTState('idle');
                return;
            }

            // Step 2: Send to session with voice prompt hint
            await this._sendVoiceText(text);

        } catch (err) {
            console.error('[SessionWindow] PTT processing failed:', err);
            this._updateStatus('error', err.message || 'Voice input failed');
        } finally {
            this._setPTTState('idle');
        }
    }

    _setPTTState(state) {
        this.pttState = state;
        if (!this.pttButton) return;

        this.pttButton.classList.remove('recording', 'processing');

        switch (state) {
            case 'recording':
                this.pttButton.classList.add('recording');
                this.pttButton.querySelector('.ptt-icon').textContent = '🔴';
                break;
            case 'processing':
                this.pttButton.classList.add('processing');
                // Keep mic icon - spinning border shows processing state
                this.pttButton.querySelector('.ptt-icon').textContent = '🎤';
                break;
            default:
                this.pttButton.querySelector('.ptt-icon').textContent = '🎤';
        }
    }

    // Activity Indicator Methods

    _setupActivityIndicator() {
        if (!this.winbox) return;

        // Find the title element in WinBox and add indicator after it
        const titleEl = this.winbox.window.querySelector('.wb-title');
        if (!titleEl) return;

        // Create indicator element
        this.activityIndicator = document.createElement('div');
        this.activityIndicator.className = 'session-activity-indicator idle';
        this.activityIndicator.innerHTML = '<div class="stop-icon"></div>';
        this.activityIndicator.title = 'Session idle';

        // Insert after title text
        titleEl.appendChild(this.activityIndicator);

        // Get the base session name (without @machine suffix) for matching events
        const baseSession = this.session.split('@')[0];

        // Subscribe to activity events for this session
        this._activityHandler = ({ session, active }) => {
            // Match on base session name (events come with just session name)
            if (session === baseSession || session === this.session) {
                // Only update if not in TTS states
                if (this.activityState !== 'generating' && this.activityState !== 'playing') {
                    this._updateActivityIndicator(active ? 'processing' : 'idle');
                }
            }
        };
        desktop.on('session_activity', this._activityHandler);

        // Subscribe to TTS events for this session
        this._ttsStartHandler = ({ session }) => {
            if (session === baseSession || session === this.session) {
                this._updateActivityIndicator('generating');
            }
        };
        desktop.on('tts_start', this._ttsStartHandler);

        this._audioHandler = ({ session }) => {
            if (session === baseSession || session === this.session) {
                this._updateActivityIndicator('playing');
            }
        };
        desktop.on('audio', this._audioHandler);

        this._audioEndedHandler = ({ session }) => {
            if (session === baseSession || session === this.session) {
                // Return to processing if timeout is active (recent activity), else idle
                if (this._activityTimeout) {
                    this._updateActivityIndicator('processing');
                } else {
                    this._updateActivityIndicator('idle');
                }
            }
        };
        desktop.on('audio_ended', this._audioEndedHandler);
    }

    _updateActivityIndicator(state) {
        if (!this.activityIndicator) return;

        this.activityState = state;
        this.activityIndicator.classList.remove('idle', 'processing', 'generating', 'playing');

        switch (state) {
            case 'processing':
                this.activityIndicator.innerHTML = '<div class="spinner"></div>';
                this.activityIndicator.title = 'Session working...';
                this.activityIndicator.classList.add('processing');
                break;
            case 'generating':
                this.activityIndicator.innerHTML = '<div class="generating-dots"><span></span><span></span><span></span></div>';
                this.activityIndicator.title = 'Generating speech...';
                this.activityIndicator.classList.add('generating');
                break;
            case 'playing':
                this.activityIndicator.innerHTML = '<div class="audio-wave"><span></span><span></span><span></span><span></span><span></span></div>';
                this.activityIndicator.title = 'Playing audio';
                this.activityIndicator.classList.add('playing');
                break;
            default:  // idle
                this.activityIndicator.innerHTML = '<div class="stop-icon"></div>';
                this.activityIndicator.title = 'Session idle';
                this.activityIndicator.classList.add('idle');
        }
    }

    /**
     * Mark session as active (received data).
     * Schedules transition to idle after threshold.
     */
    _markActivity() {
        // Don't interrupt TTS states
        if (this.activityState === 'generating' || this.activityState === 'playing') {
            return;
        }

        // Show processing state
        if (this.activityState !== 'processing') {
            this._updateActivityIndicator('processing');
        }

        // Clear existing timeout
        if (this._activityTimeout) {
            clearTimeout(this._activityTimeout);
        }

        // Schedule transition to idle
        this._activityTimeout = setTimeout(() => {
            // Don't go idle if in TTS states
            if (this.activityState !== 'generating' && this.activityState !== 'playing') {
                this._updateActivityIndicator('idle');
            }
        }, this._activityThreshold);
    }

}
