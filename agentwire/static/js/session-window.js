/**
 * session-window.js
 *
 * SessionWindow class - encapsulates a terminal window for a session.
 * Wraps WinBox window, xterm.js Terminal, and WebSocket connection.
 * Supports two modes: Monitor (read-only) and Terminal (interactive).
 *
 * Terminal mode delegates the xterm/WS core to TerminalPane (#763,
 * terminal-pane.js) — SessionWindow just owns the WinBox chrome, PTT
 * titlebar button, and activity indicator around it. Monitor mode (a
 * polling <pre> dump, not xterm) keeps its own WS/status/reconnect
 * machinery here — it's a different beast, out of scope for TerminalPane.
 */


import { apiFetch, wsProtocols } from './api.js';
import { desktop } from './desktop-manager.js';
import { sessionIcons } from './icon-manager.js';
import { buildSessionId, normalizeMachine, sameMachine } from './session-id.js';
import { ansiToHtml } from './utils/ansi.js';
import { PttController } from './ptt.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';
import { TerminalPane } from './terminal-pane.js';

// Touch-primary devices (tablets/phones) raise the on-screen keyboard the
// instant xterm's hidden textarea is focused. Opening or switching to a window
// shouldn't do that uninvited — the user taps the terminal to type, which
// focuses xterm and raises the keyboard only when they actually want it. On a
// mouse/trackpad device we still auto-focus so typing works immediately.
const TOUCH_PRIMARY = typeof window !== 'undefined'
    && window.matchMedia
    && window.matchMedia('(pointer: coarse)').matches;

// Monitor-mode WS reconnect tuning. A transient drop (portal restart, an
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
        this.pane = null;       // TerminalPane instance (terminal mode)
        this.outputEl = null;   // <pre> element (monitor mode)
        this.ws = null;         // monitor-mode WS
        this.isOpen = false;

        // Silent reconnect state for the monitor-mode WS.
        this._autoReconnectAttempts = 0;
        this._autoReconnectTimer = null;
        this._destroyed = false;       // set in close() so a stray onclose can't re-dial
        this._sessionEnded = false;    // true only when the tmux session truly ended (window closes)
        this._overlayKeyHandler = null;
        this._visibilityHandler = null; // reports tab visibility so the server can back off polling
        this._renderedLines = null;     // raw lines currently in the DOM (monitor-mode line diffing)

        // PTT (Push-to-talk) state
        this.pttButton = null;
        this.pttState = 'idle'; // idle | recording | processing (mirrors this.ptt)
        this.ptt = new PttController({
            getVoiceStatus: () => desktop.voiceStatus,
            onState: (state) => this._setPTTState(state),
            onResult: (text) => {
                if (isAutoSend()) this._sendVoiceText(text);
                else this._showTranscriptBar(text);
            },
            onError: (kind, message) => this.pane?.setStatus('error', message),
        });

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

        if (this.mode === 'terminal') {
            // Re-trigger fit after the WinBox maximize animation settles — the
            // very first onmaximize fired before the pane existed (see
            // _createWinBox's onmaximize), so that fit was a no-op.
            this.pane.fit({ afterAnimation: true, watchEl: this.winbox.window });
            this._setupPTT(container);
        } else {
            this._connectWebSocket();
            this._setupReconnectButton(container);
        }

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
                this.pane?.focus();
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
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
            this._visibilityHandler = null;
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
        if (this.pttState === 'recording') {
            this._cancelRecording();
        }

        // Terminal mode: the pane owns its own ws/xterm/resize/reconnect state.
        if (this.pane) {
            this.pane.dispose();
            this.pane = null;
        }

        // Monitor mode: close its own WS
        if (this.ws) {
            this.ws.onclose = null; // _destroyed already guards, but don't even fire
            this.ws.close();
            this.ws = null;
        }
        this.outputEl = null;

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
        if (this.mode === 'terminal' && !TOUCH_PRIMARY) {
            this.pane?.focus();
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
        }
        // Terminal mode: TerminalPane builds its own DOM directly into this
        // container (it's already sized and classed by WinBox's mount).
        return container;
    }

    _createTerminal(container) {
        if (this.mode === 'monitor') {
            // Monitor mode: just store reference to pre element
            this.outputEl = container.querySelector('.session-output');
            return;
        }

        // Terminal mode: the xterm + WS core lives in TerminalPane. PTT button
        // lives in the WinBox titlebar (see _setupPTT), not inside the pane.
        this.pane = new TerminalPane(container, {
            session: this.session,
            machine: this.machine,
            onActivity: () => this._markActivity(),
            onSessionEnded: () => {
                this._sessionEnded = true;
                this.close();
            },
        });
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
                if (this.mode === 'terminal') this.pane?.fit();
            },
            onmaximize: () => {
                // WinBox animates maximize - wait for animation to complete.
                // (Fires once synchronously during _createWinBox, before the
                // pane exists yet — pane?. no-ops that first call; open()
                // re-triggers once the pane is actually created.)
                if (this.mode === 'terminal') {
                    this.pane?.fit({ afterAnimation: true, watchEl: this.winbox.window });
                }
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
                if (this.mode === 'terminal') {
                    // Restore from minimize animates
                    this.pane?.fit({ afterAnimation: true, watchEl: this.winbox.window });
                    // Reconnect if disconnected while minimized
                    this.pane?.reconnectIfNeeded();
                } else if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
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

    /** Monitor-mode WS: /ws/{session} — JSON messages (output frames, audio,
     * lifecycle) rendered into a <pre>. Terminal mode's WS lives entirely in
     * TerminalPane; this method is monitor-only. */
    _connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const endpoint = `/ws/${this.sessionId}`;
        const url = `${protocol}//${location.host}${endpoint}`;

        // Close any existing WS (even if still CONNECTING) to avoid orphaned
        // attaches that would receive duplicate broadcast output from tmux.
        if (this.ws) {
            try { this.ws.onclose = null; this.ws.close(); } catch (e) {}
            this.ws = null;
        }

        this.ws = new WebSocket(url, wsProtocols());

        this.ws.onopen = () => {
            this._updateStatus('connected', 'Connected');
            this._hideDisconnectOverlay();

            // Healed — clear any pending silent-retry state.
            this._autoReconnectAttempts = 0;
            if (this._autoReconnectTimer) {
                clearTimeout(this._autoReconnectTimer);
                this._autoReconnectTimer = null;
            }

            // Report tab visibility so the server backs off polling when the
            // tab is hidden (#628). Listener registered once per window.
            this._sendVisibility();
            if (!this._visibilityHandler) {
                this._visibilityHandler = () => this._sendVisibility();
                document.addEventListener('visibilitychange', this._visibilityHandler);
            }
        };

        this.ws.onmessage = (event) => {
            if (!this.outputEl) return;
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'audio' && msg.data) {
                    desktop._playAudio(msg.data, this.sessionId);
                } else if (msg.type === 'speak_text' && msg.text) {
                    desktop._speakText(msg.text, this.sessionId);
                } else if (msg.type === 'remote_session_ended' || msg.type === 'local_session_ended') {
                    // tmux session truly ended (e.g. monitor-loop eviction) —
                    // close the window instead of auto-reconnecting forever
                    this._sessionEnded = true;
                    this.close();
                    return;
                } else if (msg.type === 'output' && msg.data) {
                    // Incremental line-diff render (#628) — only changed
                    // lines touch the DOM instead of a full innerHTML swap
                    this._renderOutput(msg.data);
                    this.outputEl.scrollTop = this.outputEl.scrollHeight;
                    // Mark activity when output received
                    this._markActivity();
                }
            } catch (e) {
                // Fallback: display as plain text
                this.outputEl.textContent = event.data;
                this._renderedLines = null;
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
    }

    /**
     * Monitor-mode incremental render (#628): keep one <div> per frame line
     * and only rewrite lines whose raw text changed, instead of re-parsing
     * the whole ~100-line frame via innerHTML on every update.
     * (tmux capture-pane emits SGR codes per line, so lines convert
     * independently.)
     */
    _renderOutput(data) {
        const el = this.outputEl;
        const lines = data.split('\n');
        const prev = this._renderedLines;
        if (!prev) el.textContent = '';  // clear any non-diffed content

        while (el.childNodes.length > lines.length) {
            el.removeChild(el.lastChild);
        }
        for (let i = 0; i < lines.length; i++) {
            let div = el.childNodes[i];
            if (!div) {
                div = document.createElement('div');
                el.appendChild(div);
            } else if (prev && i < prev.length && prev[i] === lines[i]) {
                continue;  // unchanged line — skip the DOM entirely
            }
            div.innerHTML = lines[i] ? ansiToHtml(lines[i]) : '<span> </span>';
        }
        this._renderedLines = lines;
    }

    _sendVisibility() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'visibility', visible: !document.hidden }));
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
     * Schedule a silent reconnect with exponential backoff (monitor-mode WS).
     * The window heals itself when the WS comes back (portal restart,
     * transient kill side-effect) instead of forcing a manual click. The
     * manual "Reconnect" overlay surfaces only after TERM_RECONNECT_OVERLAY_AFTER
     * silent attempts have failed, so a brief blip never throws up the wall.
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

    // Reconnect button handler (monitor mode; terminal mode's lives in TerminalPane)

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

    _startRecording() {
        this.ptt.start();
    }

    _stopRecording() {
        this.ptt.stop();
    }

    _cancelRecording() {
        this.ptt.cancel();
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
        this.pane?.focus();
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
            this.pane?.setStatus('connected', `Sent: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`);
            setTimeout(() => {
                if (this.pttState === 'idle') this.pane?.setStatus('connected', 'Connected');
            }, 3000);
        } catch (err) {
            console.error('[SessionWindow] Voice send failed:', err);
            this.pane?.setStatus('error', err.message || 'Voice input failed');
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
