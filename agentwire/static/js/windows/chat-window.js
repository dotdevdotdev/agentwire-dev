/**
 * Chat Window - Voice chat with orb visualization
 *
 * Provides a conversational voice interface to Claude Code sessions.
 * Features:
 * - Orb visualization showing voice states (idle, listening, processing, speaking)
 * - Session selector dropdown
 * - Push-to-talk button
 * - Chat message history
 */

import { desktop } from '../desktop-manager.js';
import { sessionIcons } from '../icon-manager.js';

/** @type {ChatWindow|null} */
let chatWindowInstance = null;

/**
 * Open the Chat window
 * @param {string} [session] - Optional session to connect to immediately
 * @returns {ChatWindow} The chat window instance
 */
export function openChatWindow(session = null) {
    if (chatWindowInstance?.winbox) {
        chatWindowInstance.winbox.focus();
        // If session provided and different from current, switch to it
        if (session && chatWindowInstance.selectedSession !== session) {
            chatWindowInstance.selectSession(session);
        }
        return chatWindowInstance;
    }

    chatWindowInstance = new ChatWindow(session);
    chatWindowInstance.open();
    return chatWindowInstance;
}

/**
 * ChatWindow class - encapsulates the chat window with orb visualization
 */
class ChatWindow {
    constructor(initialSession = null) {
        this.winbox = null;
        this.container = null;

        // State
        this.selectedSession = null;
        this._initialSession = initialSession;
        this.orbState = 'idle';
        this.messages = [];

        // Session WebSocket (triggers output polling for say detection)
        this.sessionWs = null;

        // PTT state
        this.pttState = 'idle'; // idle | recording | processing
        this.mediaRecorder = null;
        this.audioChunks = [];

        // Element references
        this.pttButton = null;
        this.orbEl = null;
        this.orbRingEl = null;
        this.stateLabelEl = null;
        this.messagesEl = null;
        this.statusIndicator = null;
        this.statusText = null;
        this.fullscreenExitBtn = null;

        // Fullscreen state
        this.isFullscreen = false;

        // Event listeners cleanup
        this._unsubscribers = [];
        this._escapeHandler = null;
    }

    /**
     * Open the chat window
     */
    open() {
        this.container = this._createContainer();
        this._createWinBox();
        this._setupEventListeners();
        this._initSession();
    }

    /**
     * Close the chat window and clean up
     */
    close() {
        // Unsubscribe from desktop events
        this._unsubscribers.forEach(unsub => unsub());
        this._unsubscribers = [];

        // Remove escape key listener
        if (this._escapeHandler) {
            document.removeEventListener('keydown', this._escapeHandler);
            this._escapeHandler = null;
        }

        // Cancel any active recording
        if (this.mediaRecorder && this.pttState === 'recording') {
            this._cancelRecording();
        }

        // Close session WebSocket
        if (this.sessionWs) {
            this.sessionWs.close();
            this.sessionWs = null;
        }

        // Clear instance reference
        if (chatWindowInstance === this) {
            chatWindowInstance = null;
        }

        this.winbox = null;
    }

    /**
     * Create the window container HTML
     */
    _createContainer() {
        const container = document.createElement('div');
        container.className = 'chat-window-content';
        container.innerHTML = `
            <div class="orb-area">
                <div class="orb-container">
                    <div class="orb idle"></div>
                    <div class="orb-ring idle"></div>
                </div>
                <div class="state-label idle">READY</div>
            </div>
            <div class="chat-messages"></div>
            <button class="chat-ptt" title="Hold to record (Ctrl+Space)">
                <span class="ptt-icon">🎤</span>
            </button>
            <div class="chat-status-bar">
                <span class="status-indicator"></span>
                <span class="status-text">No session selected</span>
            </div>
            <button class="fullscreen-exit-btn" title="Exit fullscreen (Escape)">✕</button>
        `;

        // Store element references
        this.pttButton = container.querySelector('.chat-ptt');
        this.orbEl = container.querySelector('.orb');
        this.orbRingEl = container.querySelector('.orb-ring');
        this.stateLabelEl = container.querySelector('.state-label');
        this.messagesEl = container.querySelector('.chat-messages');
        this.statusIndicator = container.querySelector('.status-indicator');
        this.statusText = container.querySelector('.status-text');
        this.fullscreenExitBtn = container.querySelector('.fullscreen-exit-btn');

        return container;
    }

    /**
     * Create the WinBox window
     */
    _createWinBox() {
        const title = this._initialSession
            ? `${this._initialSession} (chat)`
            : 'Chat';
        const icon = this._initialSession
            ? sessionIcons.getIcon(this._initialSession)
            : '/static/favicon.png';

        // Load saved position/size (null on mobile)
        const savedState = desktop.loadWindowState('chat');
        const x = savedState?.x ?? 'center';
        const y = savedState?.y ?? 'center';
        const width = savedState?.width ?? 400;
        const height = savedState?.height ?? 500;

        this.winbox = new WinBox({
            title: title,
            icon: icon,
            mount: this.container,
            root: document.getElementById('desktopArea'),
            x: x,
            y: y,
            width: width,
            height: height,
            minwidth: 300,
            minheight: 400,
            class: ['chat-window'],
            onclose: () => {
                this.close();
                return false;
            },
            onfocus: () => {
                desktop.setActiveWindow('chat');
            },
            onmove: () => {
                this._saveWindowState();
            },
            onresize: () => {
                this._saveWindowState();
            },
            onfullscreen: (isFullscreen) => {
                this._handleFullscreenChange(isFullscreen);
            }
        });

        desktop.registerWindow('chat', this.winbox);
    }

    /**
     * Save window position/size to localStorage (skip on mobile)
     */
    _saveWindowState() {
        if (!this.winbox || desktop.isNarrowViewport()) return;

        desktop.saveWindowState('chat', {
            x: this.winbox.x,
            y: this.winbox.y,
            width: this.winbox.width,
            height: this.winbox.height
        });
    }

    /**
     * Set up event listeners
     */
    _setupEventListeners() {
        // PTT button - mouse events
        this.pttButton.addEventListener('mousedown', (e) => {
            e.preventDefault();
            this._startRecording();
        });
        this.pttButton.addEventListener('mouseup', () => this._stopRecording());
        this.pttButton.addEventListener('mouseleave', () => {
            if (this.pttState === 'recording') {
                this._stopRecording();
            }
        });

        // PTT button - touch events
        this.pttButton.addEventListener('touchstart', (e) => {
            e.preventDefault();
            this._startRecording();
        });
        this.pttButton.addEventListener('touchend', () => this._stopRecording());
        this.pttButton.addEventListener('touchcancel', () => {
            if (this.pttState === 'recording') {
                this._cancelRecording();
            }
        });

        // Listen for TTS events to update orb state
        const unsubTtsStart = desktop.on('tts_start', (data) => {
            const { session, text } = data || {};
            // Match session name (could be exact or the selected session could be a prefix)
            if (session && this.selectedSession &&
                (session === this.selectedSession || session.startsWith(this.selectedSession))) {
                this._setOrbState('speaking');
                if (text) {
                    this._addMessage('assistant', text);
                }
            }
        });
        this._unsubscribers.push(unsubTtsStart);

        // Listen for audio playback completion
        const unsubAudioEnded = desktop.on('audio_ended', ({ session }) => {
            if (this.selectedSession === session || session?.startsWith(this.selectedSession)) {
                if (this.orbState === 'speaking') {
                    this._setOrbState('idle');
                }
            }
        });
        this._unsubscribers.push(unsubAudioEnded);

        // Fullscreen exit button
        this.fullscreenExitBtn.addEventListener('click', () => {
            this._exitFullscreen();
        });

        // Escape key to exit fullscreen
        this._escapeHandler = (e) => {
            if (e.key === 'Escape' && this.isFullscreen) {
                e.preventDefault();
                this._exitFullscreen();
            }
        };
        document.addEventListener('keydown', this._escapeHandler);
    }

    /**
     * Handle fullscreen state change
     */
    _handleFullscreenChange(isFullscreen) {
        this.isFullscreen = isFullscreen;
        if (isFullscreen) {
            this.container.classList.add('is-fullscreen');
        } else {
            this.container.classList.remove('is-fullscreen');
        }
    }

    /**
     * Exit fullscreen mode
     */
    _exitFullscreen() {
        if (this.winbox && this.isFullscreen) {
            this.winbox.fullscreen(false);
        }
    }

    /**
     * Initialize with the initial session if provided
     */
    _initSession() {
        if (this._initialSession) {
            this.selectSession(this._initialSession);
        } else {
            this._updateStatus();
        }
    }

    /**
     * Update the status bar
     */
    _updateStatus() {
        if (this.selectedSession) {
            this.statusIndicator.classList.add('connected');
            this.statusIndicator.classList.remove('disconnected');
            this.statusText.textContent = `Connected to ${this.selectedSession}`;
        } else {
            this.statusIndicator.classList.remove('connected');
            this.statusIndicator.classList.add('disconnected');
            this.statusText.textContent = 'No session selected';
        }
    }

    /**
     * Programmatically select a session
     * @param {string} session - Session name to select
     */
    selectSession(session) {
        if (!session) return;

        this.selectedSession = session;

        // Update WinBox title and icon
        if (this.winbox) {
            this.winbox.setTitle(`${session} (chat)`);
            this.winbox.setIcon(sessionIcons.getIcon(session));
        }

        this._connectSessionWs();
        this._updateStatus();
    }

    /**
     * Connect to session WebSocket (triggers output polling for say detection)
     */
    _connectSessionWs() {
        // Close existing connection
        if (this.sessionWs) {
            this.sessionWs.close();
            this.sessionWs = null;
        }

        if (!this.selectedSession) return;

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws/${this.selectedSession}`;

        this.sessionWs = new WebSocket(url);

        this.sessionWs.onopen = () => {
            // Connected
        };

        this.sessionWs.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);

                // Handle tts_start - set orb to speaking and add message
                if (msg.type === 'tts_start') {
                    this._setOrbState('speaking');
                    if (msg.text) {
                        this._addMessage('assistant', msg.text);
                    }
                }

                // Handle audio messages - play via desktop manager
                if (msg.type === 'audio' && msg.data) {
                    desktop._playAudio(msg.data, this.selectedSession);
                }

                // Handle output messages (terminal output from session)
                if (msg.type === 'output') {
                    // Output is handled by terminal/monitor windows
                }
            } catch (e) {
                // Not JSON, ignore
            }
        };

        this.sessionWs.onerror = (error) => {
            console.error('[ChatWindow] Session WebSocket error:', error);
        };

        this.sessionWs.onclose = () => {
            // Disconnected
        };
    }

    /**
     * Set the orb state with animation
     */
    _setOrbState(state) {
        this.orbState = state;

        // Update orb classes
        this.orbEl.className = `orb ${state}`;
        this.orbRingEl.className = `orb-ring ${state}`;

        // Update state label
        this.stateLabelEl.className = `state-label ${state}`;

        const labels = {
            idle: 'READY',
            listening: 'LISTENING',
            processing: 'PROCESSING',
            generating: 'GENERATING',
            speaking: 'SPEAKING',
            locked: 'LOCKED',
            awaiting_permission: 'AWAITING'
        };
        this.stateLabelEl.textContent = labels[state] || state.toUpperCase();
    }

    /**
     * Add a message to the chat history
     */
    _addMessage(role, text) {
        const message = { role, text, timestamp: new Date() };
        this.messages.push(message);

        const msgEl = document.createElement('div');
        msgEl.className = `chat-message ${role}`;
        msgEl.innerHTML = `
            <div class="message-text">${this._escapeHtml(text)}</div>
            <div class="timestamp">${message.timestamp.toLocaleTimeString()}</div>
        `;
        this.messagesEl.appendChild(msgEl);
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    }

    /**
     * Escape HTML special characters
     */
    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ============================================
    // PTT Recording Methods
    // ============================================

    async _startRecording() {
        if (this.pttState !== 'idle') return;
        if (!this.selectedSession) {
            this.statusText.textContent = 'Select a session first';
            return;
        }

        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            this.audioChunks = [];

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
                stream.getTracks().forEach(track => track.stop());
                if (this.audioChunks.length > 0 && this.pttState === 'processing') {
                    const blob = new Blob(this.audioChunks, { type: mimeType });
                    this._processRecording(blob);
                }
            };

            this.mediaRecorder.start();
            this._setPttState('recording');
            this._setOrbState('listening');

        } catch (err) {
            console.error('[ChatWindow] Failed to start recording:', err);
            this.statusText.textContent = 'Microphone access denied';
            this._setPttState('idle');
        }
    }

    _stopRecording() {
        if (this.pttState !== 'recording' || !this.mediaRecorder) return;

        this._setPttState('processing');
        this._setOrbState('processing');
        this.mediaRecorder.stop();
    }

    _cancelRecording() {
        if (!this.mediaRecorder) return;

        this.audioChunks = [];
        this.mediaRecorder.stop();
        this._setPttState('idle');
        this._setOrbState('idle');
    }

    async _processRecording(blob) {
        try {
            // Transcribe audio
            const formData = new FormData();
            formData.append('audio', blob, 'recording.webm');

            const transcribeRes = await fetch('/transcribe', {
                method: 'POST',
                body: formData
            });

            const transcribeData = await transcribeRes.json();

            if (transcribeData.error) {
                throw new Error(transcribeData.error);
            }

            const text = transcribeData.text?.trim();
            if (!text) {
                this.statusText.textContent = 'No speech detected';
                this._setPttState('idle');
                this._setOrbState('idle');
                return;
            }

            // Add user message to chat
            this._addMessage('user', text);

            // Send to session with voice prompt hint
            const sendRes = await fetch(`/send/${this.selectedSession}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: `[User said: '${text}' - respond using MCP tool: agentwire_say(text="your message")]`
                })
            });

            const sendData = await sendRes.json();

            if (sendData.error) {
                throw new Error(sendData.error);
            }

            this.statusText.textContent = `Sent: "${text.substring(0, 30)}${text.length > 30 ? '...' : ''}"`;

            // Transition to generating state while waiting for response
            this._setOrbState('generating');

            // Reset status after a moment if no TTS response
            setTimeout(() => {
                if (this.orbState === 'generating') {
                    this._setOrbState('idle');
                    this._updateStatus();
                }
            }, 10000);

        } catch (err) {
            console.error('[ChatWindow] Processing failed:', err);
            this.statusText.textContent = err.message || 'Voice input failed';
            this._setOrbState('idle');
        } finally {
            this._setPttState('idle');
        }
    }

    _setPttState(state) {
        this.pttState = state;

        this.pttButton.classList.remove('recording', 'processing');
        const icon = this.pttButton.querySelector('.ptt-icon');

        switch (state) {
            case 'recording':
                this.pttButton.classList.add('recording');
                if (icon) icon.textContent = '🔴';
                break;
            case 'processing':
                this.pttButton.classList.add('processing');
                // Keep mic icon - spinning border shows processing state
                if (icon) icon.textContent = '🎤';
                break;
            default:
                if (icon) icon.textContent = '🎤';
        }
    }
}
