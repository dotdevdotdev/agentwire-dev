/**
 * Desktop UI - OS-like window manager for AgentWire
 *
 * Refactored to use modular architecture:
 * - DesktopManager for WebSocket and state
 * - SessionWindow for terminal windows
 * - List windows for sessions/machines/config
 */

import { desktop } from './desktop-manager.js';
import { tileManager } from './tile-manager.js';
import { SessionWindow } from './session-window.js';
import { ArtifactWindow } from './artifact-window.js';
import { sidebar } from './sidebar.js';
import { configSection } from './sidebar/config-section.js';
import { artifactsSection } from './sidebar/artifacts-section.js';
import { machinesSection } from './sidebar/machines-section.js';
import { sessionsSection } from './sidebar/sessions-section.js';
import { sdkSessionsSection } from './sidebar/sdk-sessions-section.js';
import { SdkWatchWindow } from './windows/sdk-watch-window.js';
import { projectsSection } from './sidebar/projects-section.js';
import { schedulerSection } from './sidebar/scheduler-section.js';
import { workflowsSection } from './sidebar/workflows-section.js';
import { servicesSection } from './sidebar/services-section.js';
import { socialsSection } from './sidebar/socials-section.js';
import { notificationsPanel } from './notifications-panel.js';
import { openQuicktaskModal, isQuicktaskOpen } from './quicktask-modal.js';

// State - track open windows
const sessionWindows = new Map();  // sessionId -> SessionWindow instance
const artifactWindows = new Map();  // artifactId -> ArtifactWindow instance
const sdkWatchWindows = new Map(); // sdkSessionName -> SdkWatchWindow instance

// Global PTT state
let globalPttState = 'idle';  // idle | recording | processing
let globalMediaRecorder = null;
let globalAudioChunks = [];

// AgentWire session activity state
let agentwireSessionActive = false;

// DOM Elements (simplified - only what we need)
const elements = {
    desktopArea: document.getElementById('desktopArea'),
    // Open Windows list lives in the sidebar now (Phase 2 removed bottom taskbar).
    // Variable name kept as `taskbarWindows` internally to avoid churning every caller.
    taskbarWindows: document.getElementById('openWindowsList'),
    sidebarClock: document.getElementById('sidebarClock'),
    connectionStatus: document.getElementById('connectionStatus'),
    globalPtt: document.getElementById('sidebarGlobalPtt'),
    voiceIndicator: document.getElementById('sidebarVoiceIndicator'),
};

// Initialize
document.addEventListener('DOMContentLoaded', init);

async function init() {
    sidebar.init();
    document.getElementById('sidebarQuicktask')?.addEventListener('click', (e) => {
        e.stopPropagation();
        openQuicktaskModal();
    });
    sidebar.addSection('sessions', sessionsSection);
    sidebar.addSection('sdk-sessions', sdkSessionsSection);
    sidebar.addSection('socials', socialsSection);
    sidebar.addSection('services', servicesSection);
    sidebar.addSection('machines', machinesSection);
    sidebar.addSection('projects', projectsSection);
    sidebar.addSection('artifacts', artifactsSection);
    sidebar.addSection('scheduler', schedulerSection);
    sidebar.addSection('workflows', workflowsSection);
    sidebar.addSection('config', configSection);
    setupClock();
    setupPageUnload();
    setupGlobalPtt();
    setupWindowCycling();

    // Set up event listeners BEFORE fetching data
    desktop.on('disconnect', () => updateConnectionStatus(false));
    desktop.on('connect', () => updateConnectionStatus(true));

    // Handle tmux hook notifications
    desktop.on('session_closed', handleSessionClosed);
    desktop.on('session_created', handleSessionCreated);
    desktop.on('pane_died', handlePaneDied);
    desktop.on('session_renamed', handleSessionRenamed);
    desktop.on('window_activity', handleWindowActivity);

    // Handle TTS/audio events for voice indicator
    desktop.on('tts_start', ({ session }) => {
        if (session === 'agentwire') updateVoiceIndicator('generating');
    });
    desktop.on('audio', ({ session }) => {
        if (session === 'agentwire') updateVoiceIndicator('playing');
    });
    desktop.on('audio_ended', ({ session }) => {
        if (session === 'agentwire') {
            // Return to processing if session still active, else idle
            updateVoiceIndicator(agentwireSessionActive ? 'processing' : 'idle');
        }
    });

    // Track agentwire session processing state (triggered when message sent)
    desktop.on('session_processing', ({ session, processing }) => {
        if (session === 'agentwire') {
            agentwireSessionActive = processing;
            // Only update to processing if not in TTS states (generating/playing take priority)
            const indicator = elements.voiceIndicator;
            if (processing && indicator && !indicator.classList.contains('generating') && !indicator.classList.contains('playing')) {
                updateVoiceIndicator('processing');
            }
        }
    });

    // Track agentwire session activity for processing state
    desktop.on('session_activity', ({ session, active }) => {
        if (session === 'agentwire') {
            agentwireSessionActive = active;
            // Only update indicator if not currently in TTS states
            const indicator = elements.voiceIndicator;
            if (indicator && !indicator.classList.contains('generating') && !indicator.classList.contains('playing')) {
                updateVoiceIndicator(active ? 'processing' : 'idle');
            }
        }
    });

    await desktop.connect();
    updateConnectionStatus(true);

    // Initialize tile manager for drag-to-tile window management
    tileManager.init();

    // Set up viewport resize handling — tile-manager handles tiled windows,
    // we handle maximized session windows here (notify terminal to refit content)
    desktop.initViewportResize();
    desktop.on('viewport_resize', () => {
        const desktopArea = document.getElementById('desktopArea');
        const areaRect = desktopArea.getBoundingClientRect();

        for (const [id, sw] of sessionWindows) {
            const winbox = desktop.getWindow(id);
            if (winbox && !winbox.min) {
                if (!desktop.tileStates.has(id)) {
                    // Maximized windows: WinBox has contain:size which prevents CSS width:100%
                    // from working, so we must explicitly resize to match the viewport.
                    winbox.move(areaRect.left, areaRect.top);
                    winbox.resize(areaRect.width, areaRect.height);
                }
                sw._handleResize();
            }
        }
    });

    // Trigger terminal resize after a window is tiled
    desktop.on('window_tiled', ({ id }) => {
        if (sessionWindows.has(id)) {
            sessionWindows.get(id)._handleResizeAfterAnimation();
        }
    });

    // Desktop UI control (from MCP agents via portal API)
    desktop.on('desktop_open_window', (msg) => {
        if (msg.window_type === 'session') {
            openSessionTerminal(msg.session, msg.mode || 'monitor');
        } else if (msg.window_type === 'panel') {
            sidebar.expandSection(msg.panel);
        } else if (msg.window_type === 'artifact') {
            openArtifactWindow(msg.url, msg.title || 'Artifact', msg.artifact_id);
        }
    });

    desktop.on('desktop_close_window', ({ window_id }) => {
        const winbox = desktop.getWindow(window_id);
        if (winbox) winbox.close();
    });

    desktop.on('desktop_focus_window', ({ window_id }) => {
        desktop.setActiveWindow(window_id);
    });

    desktop.on('desktop_tile_window', ({ window_id, zone }) => {
        tileManager._tileWindow(window_id, zone);
    });

    desktop.on('desktop_minimize_all', () => {
        desktop.minimizeAllExcept(null);
    });

    desktop.on('desktop_apply_layout', ({ windows }) => {
        for (const w of windows) {
            if (w.id && w.zone) {
                tileManager._tileWindow(w.id, w.zone);
            }
        }
    });

    // Initialize notifications panel
    notificationsPanel.init();

    // Click on a toast -> open the notifications session as interactive terminal
    document.addEventListener('open-notification-session', () => {
        openSessionTerminal('agentwire-notifications', 'terminal');
    });

    // Set initial voice indicator state
    updateVoiceIndicator('idle');

    // Keep saved taskbar state in sync with minimize/restore events
    desktop.on('window_minimized', saveTaskbarState);
    desktop.on('window_restored', saveTaskbarState);

    // Restore taskbar tabs from previous page session (windows + order + active + minimized).
    // Do this BEFORE fetching sessions — restore is independent of the sessions list and
    // /api/sessions can take several seconds when remote machines need SSH probing.
    restoreTaskbarState();

    // Fetch initial data in the background (will emit events to listeners above)
    desktop.fetchSessions();
}

/**
 * Handle session_closed event from tmux hook.
 * Closes the session window if open and refreshes the sessions list.
 */
function handleSessionClosed({ session }) {
    // Close the session window if it's open
    if (sessionWindows.has(session)) {
        const sw = sessionWindows.get(session);
        sw.close();
        sessionWindows.delete(session);
        removeTaskbarButton(session);
    }

    // Sessions list will be updated by the sessions_update event
    // that the portal sends along with session_closed
}

/**
 * Handle session_created event from tmux hook.
 * Sessions list will be updated automatically via sessions_update.
 */
function handleSessionCreated({ session }) {
    // Sessions list will be updated by the sessions_update event
}

/**
 * Handle pane_died event from tmux hook.
 * Refreshes session info to update pane counts.
 */
function handlePaneDied({ session, pane_id }) {
    // Sessions list (with pane counts) will be updated by sessions_update event
}

/**
 * Handle session_renamed event from tmux hook.
 * Updates open windows and taskbar buttons with new session name.
 */
function handleSessionRenamed({ old_name, new_name }) {
    // Update session window if open
    if (old_name && sessionWindows.has(old_name)) {
        const sw = sessionWindows.get(old_name);
        sessionWindows.delete(old_name);
        sessionWindows.set(new_name, sw);

        // Update taskbar button
        removeTaskbarButton(old_name);
        addTaskbarButton(new_name, sw);
    }

    // Sessions list will be updated by sessions_update event
}

/**
 * Handle window_activity event from tmux hook.
 * Shows desktop notification for background session activity.
 */
function handleWindowActivity({ session }) {
    // Only notify if session window is not focused
    if (desktop.getActiveWindow() !== session) {
        // Request notification permission if needed
        if (Notification.permission === 'granted') {
            new Notification(`Activity in ${session}`, {
                body: 'Session has new output',
                icon: '/static/img/icon-192.png',
                tag: `activity-${session}`,  // Prevent duplicate notifications
            });
        } else if (Notification.permission !== 'denied') {
            Notification.requestPermission();
        }
    }
}

// Tab / Shift+Tab to cycle open windows
function setupWindowCycling() {
    window.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab') return;
        e.preventDefault();
        e.stopPropagation();
        const items = Array.from(elements.taskbarWindows.querySelectorAll('.sidebar-open-item'));
        if (items.length === 0) return;
        const activeId = desktop.getActiveWindow ? desktop.getActiveWindow() : null;
        const currentIndex = items.findIndex(el => el.dataset.session === activeId);
        const direction = e.shiftKey ? -1 : 1;
        const nextIndex = (currentIndex + direction + items.length) % items.length;
        const nextId = items[nextIndex]?.dataset.session;
        if (!nextId) return;
        // Focus the window and its terminal directly (don't use click which toggles minimize)
        const inst = _lookupWindowInstance(nextId);
        if (inst) {
            if (inst.isMinimized) inst.restore();
            inst.focus();
        }
        desktop.setActiveWindow(nextId);
        updateTaskbarActive(nextId);
        saveTaskbarState();
    }, true);  // capture phase — runs before xterm's handlers
}

// Clean up on page unload
function setupPageUnload() {
    window.addEventListener('beforeunload', () => {
        // Suppress taskbar state saves during teardown — we want the saved state
        // to reflect what was open, so it can be restored on next page load.
        restoringTaskbar = true;

        // Disconnect main WebSocket
        desktop.disconnect();

        // Close all windows
        sessionWindows.forEach(sw => sw.close());
        artifactWindows.forEach(aw => aw.close());
    });
}

// Clock
function setupClock() {
    function updateTime() {
        const now = new Date();
        if (elements.sidebarClock) {
            elements.sidebarClock.textContent = now.toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit'
            });
        }
    }
    updateTime();
    setInterval(updateTime, 1000);
}

// Connection status
function updateConnectionStatus(connected) {
    elements.connectionStatus.innerHTML = connected
        ? '<span class="status-dot connected"></span><span class="status-text">Connected</span>'
        : '<span class="status-dot disconnected"></span><span class="status-text">Disconnected</span>';
}

// Voice indicator - shows agentwire session and TTS activity state
function updateVoiceIndicator(state) {
    const indicator = elements.voiceIndicator;
    if (!indicator) return;

    indicator.classList.remove('idle', 'processing', 'generating', 'playing');

    switch (state) {
        case 'processing':
            indicator.innerHTML = '<div class="spinner"></div>';
            indicator.title = 'AgentWire is working...';
            indicator.classList.add('processing');
            break;
        case 'generating':
            indicator.innerHTML = '<div class="generating-dots"><span></span><span></span><span></span></div>';
            indicator.title = 'Generating speech...';
            indicator.classList.add('generating');
            break;
        case 'playing':
            indicator.innerHTML = '<div class="audio-wave"><span></span><span></span><span></span><span></span><span></span></div>';
            indicator.title = 'Playing audio';
            indicator.classList.add('playing');
            break;
        default:  // idle
            indicator.innerHTML = '<div class="stop-icon"></div>';
            indicator.title = 'AgentWire idle';
            indicator.classList.add('idle');
    }
}

/**
 * Open a session terminal window.
 * Exported for use by sessions-window.js and other modules.
 *
 * @param {string} session - Session name
 * @param {'monitor'|'terminal'} mode - Window mode
 * @param {string|null} machine - Remote machine ID (optional)
 */
export function openSessionTerminal(session, mode, machine = null) {
    const id = machine ? `${session}@${machine}` : session;

    // Check if already open — restore if minimized, otherwise focus
    if (sessionWindows.has(id)) {
        const existing = sessionWindows.get(id);
        if (existing.isMinimized) {
            if (!desktop.isTiled(id)) {
                desktop.minimizeAllExcept(id);
            }
            existing.restore();
        } else {
            existing.focus();
        }
        return;
    }

    // Minimize all other session windows before opening new one
    desktop.minimizeAllExcept(null);

    const sw = new SessionWindow({
        session,
        mode,
        machine,
        root: elements.desktopArea,
        onClose: (win) => {
            sessionWindows.delete(id);
            removeTaskbarButton(id);
            unrecordTaskbarEntry(id);
        },
        onFocus: (win) => {
            updateTaskbarActive(id);
            desktop.setActiveWindow(id);
            saveTaskbarState();
        }
    });

    sw.open();
    sessionWindows.set(id, sw);
    addTaskbarButton(id, sw);
    recordTaskbarEntry({ kind: 'session', id, session, mode, machine });
}

/**
 * Open an artifact window (agent-generated HTML or external URL).
 *
 * @param {string} url - URL or filename to load
 * @param {string} title - Window title
 * @param {string|null} artifactId - Optional explicit window ID
 */
export function openArtifactWindow(url, title = 'Artifact', artifactId = null) {
    const id = artifactId || `artifact-${url.replace(/[\/\.]/g, '-')}`;

    // Check if already open — restore if minimized, otherwise focus
    if (artifactWindows.has(id)) {
        const existing = artifactWindows.get(id);
        if (existing.isMinimized) {
            if (!desktop.isTiled(id)) {
                desktop.minimizeAllExcept(id);
            }
            existing.restore();
        } else {
            existing.focus();
        }
        return;
    }

    // Minimize all other windows before opening new one
    desktop.minimizeAllExcept(null);

    const aw = new ArtifactWindow({
        url,
        title,
        artifactId: id,
        root: elements.desktopArea,
        onClose: () => {
            artifactWindows.delete(id);
            removeTaskbarButton(id);
            unrecordTaskbarEntry(id);
        },
        onFocus: () => {
            updateTaskbarActive(id);
            desktop.setActiveWindow(id);
            saveTaskbarState();
        },
    });

    aw.open();
    artifactWindows.set(id, aw);
    addTaskbarButton(id, aw);
    recordTaskbarEntry({ kind: 'artifact', id, url, title });
}

// Taskbar management — persist open windows + order across page refresh
const TASKBAR_STATE_KEY = 'taskbar-state';
const taskbarRecords = new Map(); // id -> { kind, id, ...args }
let taskbarDragoverBound = false;
let restoringTaskbar = false;

function _lookupWindowInstance(id) {
    return sessionWindows.get(id) || artifactWindows.get(id) || sdkWatchWindows.get(id) || null;
}

/**
 * Open a watch window for a saved SDK REPL session.
 *
 * Tails ~/.agentwire/sessions/repl/<name>/transcript.jsonl over WS and
 * renders text/thinking/tool_use/tool_result events the way the Textual
 * REPL does. Read-only.
 */
export function openSdkWatchWindow(sessionName) {
    const id = `sdk-watch-${sessionName}`;
    if (sdkWatchWindows.has(id)) {
        const existing = sdkWatchWindows.get(id);
        if (existing.isMinimized) {
            if (!desktop.isTiled(id)) desktop.minimizeAllExcept(id);
            existing.restore();
        } else {
            existing.focus();
        }
        return;
    }
    desktop.minimizeAllExcept(null);
    const w = new SdkWatchWindow({
        session: sessionName,
        windowId: id,
        root: elements.desktopArea,
        onClose: () => {
            sdkWatchWindows.delete(id);
            removeTaskbarButton(id);
            unrecordTaskbarEntry(id);
        },
        onFocus: () => {
            updateTaskbarActive(id);
            desktop.setActiveWindow(id);
            saveTaskbarState();
        },
    });
    w.open();
    sdkWatchWindows.set(id, w);
    addTaskbarButton(id, w);
    recordTaskbarEntry({ kind: 'sdk-watch', id, sessionName });
}

function loadTaskbarState() {
    try {
        const raw = localStorage.getItem(TASKBAR_STATE_KEY);
        if (!raw) return { tabs: [], activeId: null };
        const data = JSON.parse(raw);
        if (Array.isArray(data)) return { tabs: data, activeId: null };  // legacy schema
        return {
            tabs: Array.isArray(data.tabs) ? data.tabs : [],
            activeId: data.activeId || null,
        };
    } catch (e) {
        return { tabs: [], activeId: null };
    }
}

function saveTaskbarState() {
    if (restoringTaskbar) return;
    const ids = Array.from(elements.taskbarWindows.querySelectorAll('.sidebar-open-item'))
        .map(btn => btn.dataset.session);
    const tabs = ids.map(id => {
        const rec = taskbarRecords.get(id);
        if (!rec) return null;
        const inst = _lookupWindowInstance(id);
        // For placeholder records (no live instance), trust the record's saved minimized flag.
        const minimized = inst ? !!inst.isMinimized : !!rec.minimized;
        return { ...rec, minimized };
    }).filter(Boolean);
    const activeId = desktop.getActiveWindow ? desktop.getActiveWindow() : null;
    try {
        localStorage.setItem(TASKBAR_STATE_KEY, JSON.stringify({ tabs, activeId }));
    } catch (e) {}
}

function recordTaskbarEntry(record) {
    taskbarRecords.set(record.id, record);
    saveTaskbarState();
}

function unrecordTaskbarEntry(id) {
    taskbarRecords.delete(id);
    saveTaskbarState();
}

function _openByRecord(rec) {
    if (rec.kind === 'session') {
        openSessionTerminal(rec.session, rec.mode || 'monitor', rec.machine || null);
    } else if (rec.kind === 'artifact') {
        openArtifactWindow(rec.url, rec.title || 'Artifact', rec.id);
    }
}

export function restoreTaskbarState() {
    const { tabs: rawTabs, activeId } = loadTaskbarState();
    // Phase 3 moved panels to sidebar accordions — filter out stale panel records.
    const tabs = rawTabs.filter(t => t.kind !== 'panel');
    if (tabs.length === 0) return;
    restoringTaskbar = true;
    try {
        // Choose which window to actually construct now: saved active, else last in list.
        const focusRec = (activeId && tabs.find(t => t.id === activeId)) || tabs[tabs.length - 1];
        // Build placeholders first so they occupy the correct DOM order, then materialize the focus one.
        for (const rec of tabs) {
            if (rec.id === focusRec.id) continue;
            addPlaceholderTaskbarButton(rec);
        }
        // Open the focus window for real. The minimizeAllExcept inside its open path
        // is harmless because no real windows exist yet.
        try {
            _openByRecord(focusRec);
        } catch (e) {
            console.warn('[taskbar] Failed to restore focus window', focusRec, e);
        }
        // Move the focus button into its saved DOM slot (open* appends to end of taskbar)
        const focusBtn = elements.taskbarWindows.querySelector(`[data-session="${CSS.escape(focusRec.id)}"]`);
        const focusIndex = tabs.findIndex(t => t.id === focusRec.id);
        if (focusBtn && focusIndex >= 0) {
            const refBtn = elements.taskbarWindows.querySelectorAll('.sidebar-open-item')[focusIndex];
            if (refBtn && refBtn !== focusBtn) {
                elements.taskbarWindows.insertBefore(focusBtn, refBtn);
            }
        }
    } finally {
        restoringTaskbar = false;
        saveTaskbarState();
    }
}

function addPlaceholderTaskbarButton(rec) {
    const id = rec.id;
    if (elements.taskbarWindows.querySelector(`[data-session="${CSS.escape(id)}"]`)) return;
    const btn = document.createElement('div');
    btn.className = 'sidebar-open-item minimized';
    btn.dataset.session = id;
    btn.draggable = true;

    const titleEl = document.createElement('span');
    titleEl.className = 'sidebar-open-item-title';
    titleEl.textContent = rec.session || rec.title || rec.panel || id;
    btn.appendChild(titleEl);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'sidebar-open-item-close';
    closeBtn.type = 'button';
    closeBtn.title = 'Remove';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('mousedown', (e) => e.stopPropagation());
    closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        btn.remove();
        unrecordTaskbarEntry(id);
    });
    btn.appendChild(closeBtn);

    btn.addEventListener('click', () => materializePlaceholder(btn, rec));
    btn.addEventListener('dragstart', (e) => {
        btn.classList.add('dragging');
        if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', id);
        }
    });
    btn.addEventListener('dragend', () => {
        btn.classList.remove('dragging');
        saveTaskbarState();
    });
    elements.taskbarWindows.appendChild(btn);
    bindTaskbarDragover();
    // Pre-populate record so saveTaskbarState includes the placeholder.
    taskbarRecords.set(id, { ...rec, minimized: true });
}

function materializePlaceholder(btn, rec) {
    if (btn._materialized) return;
    btn._materialized = true;
    const id = rec.id;
    const nextSibling = btn.nextSibling;
    btn.remove();
    try {
        _openByRecord(rec);
    } catch (e) {
        console.warn('[taskbar] Failed to materialize placeholder', rec, e);
        return;
    }
    // Move the freshly-created real button to the placeholder's slot.
    const newBtn = elements.taskbarWindows.querySelector(`[data-session="${CSS.escape(id)}"]`);
    if (newBtn && nextSibling && newBtn !== nextSibling) {
        elements.taskbarWindows.insertBefore(newBtn, nextSibling);
        saveTaskbarState();
    }
}

function bindTaskbarDragover() {
    if (taskbarDragoverBound) return;
    taskbarDragoverBound = true;
    elements.taskbarWindows.addEventListener('dragover', (e) => {
        e.preventDefault();
        const dragging = elements.taskbarWindows.querySelector('.sidebar-open-item.dragging');
        if (!dragging) return;
        const target = e.target.closest('.sidebar-open-item');
        if (!target || target === dragging) return;
        const rect = target.getBoundingClientRect();
        // Vertical reorder: insert before/after based on midpoint of height.
        const after = (e.clientY - rect.top) > rect.height / 2;
        target.parentNode.insertBefore(dragging, after ? target.nextSibling : target);
    });
}

function addTaskbarButton(id, windowInstance) {
    const btn = document.createElement('div');
    btn.className = 'sidebar-open-item active';
    btn.dataset.session = id;
    btn.draggable = true;

    const titleEl = document.createElement('span');
    titleEl.className = 'sidebar-open-item-title';
    titleEl.textContent = windowInstance.title || windowInstance.session || id;
    btn.appendChild(titleEl);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'sidebar-open-item-close';
    closeBtn.type = 'button';
    closeBtn.title = 'Close window';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('mousedown', (e) => e.stopPropagation());
    closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (typeof windowInstance.close === 'function') {
            windowInstance.close();
        }
    });
    btn.appendChild(closeBtn);

    btn.addEventListener('click', () => {
        if (windowInstance.isMinimized) {
            // Skip minimizeAllExcept for tiled windows — they restore to their tile position
            if (!desktop.isTiled(id)) {
                desktop.minimizeAllExcept(id);
            }
            windowInstance.restore();
        } else {
            // Minimize this window
            windowInstance.minimize();
        }
    });
    btn.addEventListener('dragstart', (e) => {
        btn.classList.add('dragging');
        if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', id);
        }
    });
    btn.addEventListener('dragend', () => {
        btn.classList.remove('dragging');
        saveTaskbarState();
    });
    elements.taskbarWindows.appendChild(btn);
    bindTaskbarDragover();

    // Listen for minimize events to update tab styling
    desktop.on('window_minimized', ({ id: minimizedId }) => {
        if (minimizedId === id) {
            btn.classList.remove('active');
            btn.classList.add('minimized');
        }
    });
}

function removeTaskbarButton(id) {
    const btn = elements.taskbarWindows.querySelector(`[data-session="${CSS.escape(id)}"]`);
    if (btn) btn.remove();
}

function updateTaskbarActive(id) {
    elements.taskbarWindows.querySelectorAll('.sidebar-open-item').forEach(btn => {
        if (btn.dataset.session === id) {
            btn.classList.add('active');
            btn.classList.remove('minimized');
        } else {
            btn.classList.remove('active');
            btn.classList.add('minimized');
        }
    });
}

// Global PTT - always sends to "agentwire" session
function setupGlobalPtt() {
    const btn = elements.globalPtt;
    if (!btn) return;

    // Mouse events
    btn.addEventListener('mousedown', startGlobalRecording);
    btn.addEventListener('mouseup', stopGlobalRecording);
    btn.addEventListener('mouseleave', stopGlobalRecording);

    // Touch events for mobile
    btn.addEventListener('touchstart', (e) => {
        e.preventDefault();
        startGlobalRecording();
    });
    btn.addEventListener('touchend', (e) => {
        e.preventDefault();
        stopGlobalRecording();
    });

    // Global keyboard shortcut (Ctrl/Cmd + Space)
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.code === 'Space' && globalPttState === 'idle') {
            e.preventDefault();
            startGlobalRecording();
        }
        // Cmd/Ctrl + K opens the Quicktask modal. xterm.js uses a hidden textarea
        // for terminal input, so we don't skip on tag — Cmd+K is always intercepted.
        if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            if (!isQuicktaskOpen()) openQuicktaskModal();
        }
    });
    document.addEventListener('keyup', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.code === 'Space' && globalPttState === 'recording') {
            e.preventDefault();
            stopGlobalRecording();
        }
    });
}

async function startGlobalRecording() {
    if (globalPttState !== 'idle') return;

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        globalMediaRecorder = new MediaRecorder(stream, {
            mimeType: 'audio/webm;codecs=opus'
        });

        globalAudioChunks = [];
        globalMediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) globalAudioChunks.push(e.data);
        };

        globalMediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            if (globalAudioChunks.length > 0) {
                await processGlobalRecording();
            }
        };

        globalMediaRecorder.start();
        updateGlobalPttState('recording');
    } catch (err) {
        console.error('[GlobalPTT] Failed to start recording:', err);
    }
}

function stopGlobalRecording() {
    if (globalPttState !== 'recording' || !globalMediaRecorder) return;
    globalMediaRecorder.stop();
    updateGlobalPttState('processing');
}

async function processGlobalRecording() {
    try {
        const blob = new Blob(globalAudioChunks, { type: 'audio/webm' });
        const formData = new FormData();
        formData.append('audio', blob, 'recording.webm');

        // Transcribe
        const transcribeRes = await fetch('/transcribe', {
            method: 'POST',
            body: formData
        });
        const { text } = await transcribeRes.json();

        if (text && text.trim()) {
            // Send to agentwire session with voice prompt
            await fetch('/send/agentwire', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: `[User said: '${text}' - respond using MCP tool: agentwire_say(text="your message")]`
                })
            });
        }
    } catch (err) {
        console.error('[GlobalPTT] Processing failed:', err);
    } finally {
        updateGlobalPttState('idle');
    }
}

function updateGlobalPttState(state) {
    globalPttState = state;
    const btn = elements.globalPtt;
    if (!btn) return;

    btn.classList.remove('recording', 'processing');
    const icon = btn.querySelector('.ptt-icon');

    switch (state) {
        case 'recording':
            btn.classList.add('recording');
            if (icon) icon.textContent = '🔴';
            break;
        case 'processing':
            btn.classList.add('processing');
            // Keep mic icon - spinning border shows processing state
            if (icon) icon.textContent = '🎤';
            break;
        default:
            if (icon) icon.textContent = '🎤';
    }
}
