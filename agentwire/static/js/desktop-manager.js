/**
 * Desktop Manager - Singleton for managing the desktop environment.
 *
 * Provides:
 * - Single WebSocket connection shared by all windows
 * - Window registry for tracking open WinBox instances
 * - State management (sessions, machines, config)
 * - Event dispatching for state updates
 *
 * @module desktop-manager
 */

// Reconnect configuration
const INITIAL_RECONNECT_DELAY = 1000;
const MAX_RECONNECT_DELAY = 30000;
const RECONNECT_MULTIPLIER = 1.5;

class DesktopManager {
    constructor() {
        /** @type {WebSocket|null} */
        this.ws = null;

        /** @type {Map<string, WinBox>} */
        this.windows = new Map();

        /** @type {Array<Object>} */
        this.sessions = [];

        /** @type {Array<Object>} */
        this.machines = [];

        /** @type {Object} */
        this.config = {};

        /** @type {Map<string, Set<Function>>} */
        this.listeners = new Map();

        /** @type {number} */
        this.reconnectAttempts = 0;

        /** @type {number|null} */
        this.reconnectTimeout = null;

        /** @type {boolean} */
        this.intentionalDisconnect = false;

        /** @type {string|null} */
        this.activeWindow = null;

        /** @type {Map<string, string>} windowId -> zone string for tiled windows */
        this.tileStates = new Map();

        /** @type {AudioContext|null} */
        this._audioContext = null;

        /** @type {string|null} Device-level audio dedupe */
        this._lastAudioHash = null;

        /** @type {number} Timestamp of last audio play */
        this._lastAudioTime = 0;

        /** @type {Array<{base64Data: string, session: string}>} Audio queue */
        this._audioQueue = [];

        /** @type {boolean} Whether audio is currently playing */
        this._audioPlaying = false;
    }

    // ============================================
    // Lifecycle
    // ============================================

    /**
     * Connect to the WebSocket server.
     * @returns {Promise<void>}
     */
    async connect() {
        this.intentionalDisconnect = false;

        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws`;

        try {
            this.ws = new WebSocket(url);
        } catch (err) {
            console.error('[DesktopManager] Failed to create WebSocket:', err);
            this.scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            this.reconnectAttempts = 0;
            this.emit('connect');
        };

        this.ws.onclose = (event) => {
            this.ws = null;
            this.emit('disconnect');

            if (!this.intentionalDisconnect) {
                this.scheduleReconnect();
            }
        };

        this.ws.onerror = (error) => {
            console.error('[DesktopManager] WebSocket error:', error);
        };

        this.ws.onmessage = (event) => {
            this.handleMessage(event);
        };
    }

    /**
     * Disconnect from the WebSocket server.
     */
    disconnect() {
        this.intentionalDisconnect = true;

        if (this.reconnectTimeout) {
            clearTimeout(this.reconnectTimeout);
            this.reconnectTimeout = null;
        }

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }

        this.reconnectAttempts = 0;
    }

    /**
     * Calculate reconnect delay with exponential backoff.
     * @returns {number} Delay in milliseconds
     */
    getReconnectDelay() {
        return Math.min(
            INITIAL_RECONNECT_DELAY * Math.pow(RECONNECT_MULTIPLIER, this.reconnectAttempts),
            MAX_RECONNECT_DELAY
        );
    }

    /**
     * Schedule a reconnection attempt.
     */
    scheduleReconnect() {
        if (this.intentionalDisconnect) return;

        const delay = this.getReconnectDelay();
        this.reconnectAttempts++;

        this.reconnectTimeout = setTimeout(() => {
            this.reconnectTimeout = null;
            if (!this.intentionalDisconnect) {
                this.connect();
            }
        }, delay);
    }

    /**
     * Handle incoming WebSocket messages.
     * @param {MessageEvent} event
     */
    handleMessage(event) {
        let msg;
        try {
            msg = JSON.parse(event.data);
        } catch (err) {
            console.error('[DesktopManager] Failed to parse message:', err);
            return;
        }

        switch (msg.type) {
            case 'sessions_update':
                this.sessions = msg.sessions || [];
                this.emit('sessions', this.sessions);
                break;

            case 'machines_update':
                this.machines = msg.machines || [];
                this.emit('machines', this.machines);
                break;

            case 'config_update':
                this.config = msg.config || {};
                this.emit('config', this.config);
                break;

            case 'session_activity':
                this.emit('session_activity', {
                    session: msg.session,
                    active: msg.active
                });
                break;

            case 'session_created':
                this.emit('session_created', { session: msg.session });
                break;

            case 'session_closed':
                this.emit('session_closed', { session: msg.session });
                break;

            case 'pane_died':
                this.emit('pane_died', { session: msg.session, pane_id: msg.pane_id });
                break;

            case 'pane_created':
                this.emit('pane_created', { session: msg.session, pane_id: msg.pane_id });
                break;

            case 'client_attached':
                this.emit('client_attached', {
                    session: msg.session,
                    client_count: msg.client_count
                });
                break;

            case 'client_detached':
                this.emit('client_detached', {
                    session: msg.session,
                    client_count: msg.client_count
                });
                break;

            case 'session_renamed':
                this.emit('session_renamed', {
                    old_name: msg.old_name,
                    new_name: msg.new_name
                });
                break;

            case 'pane_focused':
                this.emit('pane_focused', {
                    session: msg.session,
                    pane_id: msg.pane_id
                });
                break;

            case 'window_activity':
                this.emit('window_activity', { session: msg.session });
                break;

            case 'session_processing':
                this.emit('session_processing', { session: msg.session, processing: msg.processing });
                break;

            case 'tts_start':
                this.emit('tts_start', { session: msg.session, text: msg.text });
                break;

            case 'audio':
                // Audio messages from sessions - play through desktop audio system
                if (msg.data) {
                    this._playAudio(msg.data, msg.session);
                }
                this.emit('audio', { session: msg.session });
                break;

            case 'audio_playing':
                // Lightweight notification that audio is playing (no data)
                // Used for activity indicators without triggering playback
                this.emit('audio', { session: msg.session });
                break;

            case 'audio_done':
                // Server notification that audio playback should be complete
                // Used for activity indicators on devices that didn't play the audio
                this.emit('audio_ended', { session: msg.session });
                break;

            // Desktop UI control (from MCP agents via portal API)
            case 'desktop_report_windows': {
                // Server asking us to report our open windows
                const windows = [];
                for (const [id, winbox] of this.windows) {
                    windows.push({
                        id,
                        title: winbox.title || id,
                        type: this.tileStates.has(id) ? 'tiled' : (winbox.max ? 'maximized' : 'normal'),
                        zone: this.tileStates.get(id) || null,
                        minimized: !!winbox.min,
                    });
                }
                this.send('desktop_windows_report', {
                    request_id: msg.request_id,
                    windows,
                });
                break;
            }

            case 'desktop_open_window':
                this.emit('desktop_open_window', msg);
                break;

            case 'desktop_close_window':
                this.emit('desktop_close_window', { window_id: msg.window_id });
                break;

            case 'desktop_focus_window':
                this.emit('desktop_focus_window', { window_id: msg.window_id });
                break;

            case 'desktop_tile_window':
                this.emit('desktop_tile_window', { window_id: msg.window_id, zone: msg.zone });
                break;

            case 'desktop_minimize_all':
                this.emit('desktop_minimize_all', {});
                break;

            case 'desktop_collage':
                this.emit('desktop_collage', {});
                break;

            case 'desktop_apply_layout':
                this.emit('desktop_apply_layout', { windows: msg.windows });
                break;

            case 'scheduler_update':
                this.emit('scheduler_update', msg);
                break;

            case 'scheduler_state':
                this.emit('scheduler_state', msg);
                break;

            case 'agent_progress':
                this.emit('agent_progress', msg);
                break;

            // Desktop notifications (from MCP agents via portal API)
            case 'notification':
                this.emit('notification', msg);
                break;

            case 'notification_dismiss':
                this.emit('notification_dismiss', { id: msg.id });
                break;

            case 'scratchpad_updated':
                this.emit('scratchpad_updated', { notes: msg.notes || [] });
                break;

            default:
                // Unknown message types are silently ignored
        }
    }

    // ============================================
    // Window Management
    // ============================================

    /** @type {number} Viewport width threshold for narrow mode */
    static NARROW_VIEWPORT_WIDTH = 600;

    /**
     * Check if we're on a narrow viewport (mobile/tablet).
     * @returns {boolean}
     */
    isNarrowViewport() {
        return window.innerWidth < DesktopManager.NARROW_VIEWPORT_WIDTH;
    }

    /**
     * Safely call a WinBox method, handling invalid states.
     * @param {WinBox} winbox - WinBox instance
     * @param {string} method - Method name ('minimize', 'maximize', 'focus')
     * @param {string} [windowId] - Window ID for logging/cleanup
     * @returns {boolean} True if successful, false if failed
     * @private
     */
    _safeWinBoxOp(winbox, method, windowId = null) {
        if (!winbox || !winbox.body || typeof winbox[method] !== 'function') {
            return false;
        }
        try {
            winbox[method]();
            return true;
        } catch (e) {
            console.warn(`[DesktopManager] Failed to ${method}${windowId ? ` ${windowId}` : ''}, removing from registry:`, e);
            if (windowId) {
                this.windows.delete(windowId);
            }
            return false;
        }
    }

    /**
     * Register a window with the manager.
     * Auto-minimizes other windows and maximizes the new one.
     * @param {string} id - Window identifier
     * @param {WinBox} winbox - WinBox instance
     */
    registerWindow(id, winbox) {
        this.windows.set(id, winbox);

        // Always use single-window mode: minimize others, maximize this one
        this.minimizeAllExcept(id);
        if (winbox && !winbox.max) {
            this._safeWinBoxOp(winbox, 'maximize', id);
        }

        this.emit('window_registered', { id, winbox });
    }

    /**
     * Minimize all windows except the specified one.
     * @param {string} exceptId - Window ID to keep open (optional)
     */
    minimizeAllExcept(exceptId = null) {
        for (const [windowId, winbox] of this.windows) {
            if (windowId === exceptId) continue;
            if (this.tileStates.has(windowId)) continue;  // Skip tiled windows
            if (winbox && !winbox.min) {
                this._safeWinBoxOp(winbox, 'minimize', windowId);
            }
        }
    }

    /**
     * Unregister a window from the manager.
     * @param {string} id - Window identifier
     */
    unregisterWindow(id) {
        this.windows.delete(id);
        this.tileStates.delete(id);
        if (this.activeWindow === id) {
            this.activeWindow = null;
        }
        this.emit('window_unregistered', { id });
    }

    // ============================================
    // Window State Persistence
    // ============================================

    /** @type {string} localStorage key prefix for window states */
    static WINDOW_STATE_PREFIX = 'agentwire_window_';

    /**
     * Save window position and size to localStorage.
     * @param {string} id - Window identifier
     * @param {Object} state - {x, y, width, height}
     */
    saveWindowState(id, state) {
        try {
            const key = DesktopManager.WINDOW_STATE_PREFIX + id;
            localStorage.setItem(key, JSON.stringify(state));
        } catch (e) {
            console.warn('[DesktopManager] Failed to save window state:', e);
        }
    }

    /**
     * Load window position and size from localStorage.
     * Returns null on narrow viewports (mobile uses maximized mode).
     * @param {string} id - Window identifier
     * @returns {Object|null} {x, y, width, height} or null
     */
    loadWindowState(id) {
        // Don't restore position on mobile - it uses maximized mode
        if (this.isNarrowViewport()) {
            return null;
        }

        try {
            const key = DesktopManager.WINDOW_STATE_PREFIX + id;
            const data = localStorage.getItem(key);
            if (data) {
                const state = JSON.parse(data);
                // Validate state has required fields
                if (typeof state.x === 'number' && typeof state.y === 'number') {
                    return state;
                }
            }
        } catch (e) {
            console.warn('[DesktopManager] Failed to load window state:', e);
        }
        return null;
    }

    /**
     * Clear all saved window states from localStorage.
     */
    clearWindowStates() {
        const prefix = DesktopManager.WINDOW_STATE_PREFIX;
        const keysToRemove = [];

        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key && key.startsWith(prefix)) {
                keysToRemove.push(key);
            }
        }

        keysToRemove.forEach(key => localStorage.removeItem(key));
    }

    /**
     * Get a registered window by ID.
     * @param {string} id - Window identifier
     * @returns {WinBox|undefined}
     */
    getWindow(id) {
        return this.windows.get(id);
    }

    /**
     * Check if a window is registered.
     * @param {string} id - Window identifier
     * @returns {boolean}
     */
    hasWindow(id) {
        return this.windows.has(id);
    }

    /**
     * Check if a session window (terminal/monitor) is open for a session.
     * Used to prevent double audio playback from dashboard + session window.
     * @param {string} session - Session name
     * @returns {boolean}
     */
    _hasSessionWindow(session) {
        // Session windows are registered with their session name as ID
        // Also check for @machine variants
        if (this.windows.has(session)) return true;

        // Check if any window key starts with the session name (handles session@machine)
        for (const key of this.windows.keys()) {
            if (key === session || key.startsWith(`${session}@`)) {
                return true;
            }
        }
        return false;
    }

    /**
     * Check if a window is tiled.
     * @param {string} id - Window identifier
     * @returns {boolean}
     */
    isTiled(id) {
        return this.tileStates.has(id);
    }

    /**
     * Get all registered windows.
     * @returns {Map<string, WinBox>}
     */
    getAllWindows() {
        return this.windows;
    }

    /**
     * Set the active window.
     * On narrow viewports, also maximizes the window and minimizes others.
     * @param {string|null} id - Window identifier
     */
    setActiveWindow(id) {
        this.activeWindow = id;

        if (id) {
            const winbox = this.windows.get(id);
            if (winbox) {
                if (this.tileStates.has(id)) {
                    // Tiled window: just focus, don't minimize others or maximize
                    this._safeWinBoxOp(winbox, 'focus', id);
                } else {
                    // Normal: minimize non-tiled others, maximize this
                    this.minimizeAllExcept(id);
                    if (!winbox.max) {
                        this._safeWinBoxOp(winbox, 'maximize', id);
                    }
                }
            }
        }

        this.emit('active_window_changed', { id });
    }

    /**
     * Get the active window ID.
     * @returns {string|null}
     */
    getActiveWindow() {
        return this.activeWindow;
    }

    /**
     * Set up viewport resize listener.
     * Emits 'viewport_resize' event after resize settles so consumers
     * (tile-manager, session windows) can respond accordingly.
     */
    initViewportResize() {
        let resizeTimer = null;

        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
                this.emit('viewport_resize', {});
            }, 100);
        });
    }

    // ============================================
    // Data Access
    // ============================================

    /**
     * Fetch sessions from the API.
     * @returns {Promise<Array<Object>>}
     */
    async fetchSessions() {
        // Local sessions first (fast), then merge remote when SSH completes.
        try {
            const localRes = await fetch('/api/sessions/local');
            const localData = await localRes.json();
            this.sessions = localData.sessions || [];
            this.emit('sessions', this.sessions);
        } catch (error) {
            console.error('[DesktopManager] Failed to fetch local sessions:', error);
        }

        // Fire-and-forget: merge remote sessions when they arrive
        fetch('/api/sessions/remote').then(async (res) => {
            try {
                const data = await res.json();
                const remote = data.sessions || [];
                if (remote.length) {
                    const localNames = new Set(this.sessions.map(s => s.name));
                    for (const s of remote) {
                        if (!localNames.has(s.name)) this.sessions.push(s);
                    }
                    this.emit('sessions', this.sessions);
                }
            } catch (e) {}
        }).catch(() => {});

        return this.sessions;
    }

    /**
     * Fetch machines from the API.
     * @returns {Promise<Array<Object>>}
     */
    async fetchMachines() {
        try {
            const response = await fetch('/api/machines');
            const data = await response.json();

            // API returns array directly, not {machines: [...]}
            this.machines = Array.isArray(data) ? data : (data.machines || []);
            this.emit('machines', this.machines);
            return this.machines;
        } catch (error) {
            console.error('[DesktopManager] Failed to fetch machines:', error);
            return this.machines;
        }
    }

    /**
     * Fetch config from the API.
     * @returns {Promise<Object>}
     */
    async fetchConfig() {
        try {
            const response = await fetch('/api/config');
            const data = await response.json();

            this.config = data || {};
            this.emit('config', this.config);
            return this.config;
        } catch (error) {
            console.error('[DesktopManager] Failed to fetch config:', error);
            return this.config;
        }
    }

    /**
     * Get cached sessions.
     * @returns {Array<Object>}
     */
    getSessions() {
        return this.sessions;
    }

    /**
     * Get cached machines.
     * @returns {Array<Object>}
     */
    getMachines() {
        return this.machines;
    }

    /**
     * Get cached config.
     * @returns {Object}
     */
    getConfig() {
        return this.config;
    }

    /**
     * Find a session by name.
     * @param {string} name - Session name
     * @returns {Object|undefined}
     */
    getSession(name) {
        return this.sessions.find(s => s.name === name);
    }

    /**
     * Find a machine by ID.
     * @param {string} id - Machine ID
     * @returns {Object|undefined}
     */
    getMachine(id) {
        return this.machines.find(m => m.id === id);
    }

    // ============================================
    // WebSocket Communication
    // ============================================

    /**
     * Send a message through the WebSocket.
     * @param {string} type - Message type
     * @param {Object} data - Message data
     * @returns {boolean} True if sent successfully
     */
    send(type, data = {}) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('[DesktopManager] Cannot send - not connected');
            return false;
        }

        try {
            this.ws.send(JSON.stringify({ type, ...data }));
            return true;
        } catch (err) {
            console.error('[DesktopManager] Send failed:', err);
            return false;
        }
    }

    /**
     * Check if WebSocket is connected.
     * @returns {boolean}
     */
    isConnected() {
        return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
    }

    // ============================================
    // Event System
    // ============================================

    /**
     * Subscribe to an event.
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     * @returns {Function} Unsubscribe function
     */
    on(event, callback) {
        if (!this.listeners.has(event)) {
            this.listeners.set(event, new Set());
        }
        this.listeners.get(event).add(callback);

        // Return unsubscribe function
        return () => this.off(event, callback);
    }

    /**
     * Subscribe to an event for a single emission, then auto-unsubscribe.
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     * @returns {Function} Unsubscribe function
     */
    once(event, callback) {
        const wrapper = (data) => {
            this.off(event, wrapper);
            callback(data);
        };
        return this.on(event, wrapper);
    }

    /**
     * Unsubscribe from an event.
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     */
    off(event, callback) {
        const callbacks = this.listeners.get(event);
        if (callbacks) {
            callbacks.delete(callback);
        }
    }

    /**
     * Emit an event to all subscribers.
     * @param {string} event - Event name
     * @param {*} data - Event data
     */
    emit(event, data) {
        const callbacks = this.listeners.get(event);
        if (callbacks) {
            callbacks.forEach(callback => {
                try {
                    callback(data);
                } catch (err) {
                    console.error(`[DesktopManager] Error in ${event} handler:`, err);
                }
            });
        }
    }

    // ============================================
    // Audio Playback (Queued)
    // ============================================

    /**
     * Queue base64-encoded audio data for sequential playback.
     * Messages queue and play one after another without overlap.
     * @param {string} base64Data - Base64 encoded audio (WAV format)
     * @param {string} session - Session name for event emission
     */
    async _playAudio(base64Data, session) {
        if (!base64Data) {
            console.warn('[DesktopManager] No audio data to play');
            return;
        }

        // Device-level dedupe: hash first 100 chars + length
        const audioHash = `${base64Data.substring(0, 100)}-${base64Data.length}`;
        const now = Date.now();

        // Skip if same audio within 2 seconds (multiple windows, same device)
        if (audioHash === this._lastAudioHash && (now - this._lastAudioTime) < 2000) {
            return;
        }

        this._lastAudioHash = audioHash;
        this._lastAudioTime = now;

        // Add to queue
        this._audioQueue.push({ base64Data, session });

        // Start playback if not already playing
        if (!this._audioPlaying) {
            this._playNextAudio();
        }
    }

    /**
     * Play the next audio in the queue.
     * Called when audio ends or when first item is queued.
     */
    async _playNextAudio() {
        if (this._audioQueue.length === 0) {
            this._audioPlaying = false;
            return;
        }

        this._audioPlaying = true;
        const { base64Data, session } = this._audioQueue.shift();

        try {
            // Decode base64 to binary
            const binaryString = atob(base64Data);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }

            // Create or resume AudioContext
            if (!this._audioContext) {
                this._audioContext = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (this._audioContext.state === 'suspended') {
                await this._audioContext.resume();
            }

            // Decode and play
            const audioBuffer = await this._audioContext.decodeAudioData(bytes.buffer.slice(0));
            const source = this._audioContext.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(this._audioContext.destination);

            // When playback ends, wait 300ms then play next
            source.onended = () => {
                this.emit('audio_ended', { session });
                setTimeout(() => this._playNextAudio(), 300);
            };

            source.start(0);
        } catch (err) {
            console.error('[DesktopManager] Audio playback failed:', err);
            // On error, try next in queue after short delay
            setTimeout(() => this._playNextAudio(), 100);
        }
    }
}

// Export singleton instance
export const desktop = new DesktopManager();
