/**
 * Desktop UI - OS-like window manager for AgentWire
 *
 * Refactored to use modular architecture:
 * - DesktopManager for WebSocket and state
 * - SessionWindow for terminal windows
 * - List windows for sessions/machines/config
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';
import { tileManager } from './tile-manager.js';
import { collage } from './collage.js';
import { SessionWindow } from './session-window.js';
import { ArtifactWindow } from './artifact-window.js';
import { ReviewWindow } from './review-window.js';
import { CouncilWindow, COUNCIL_WINDOW_ID } from './council-window.js';
import { sidebar } from './sidebar.js';
import { buildSessionId, normalizeMachine, isLocalMachine } from './session-id.js';
import { notificationsActive } from './notification-prefs.js';
import { attachHorizontalSwipe } from './utils/swipe.js';
import { configSection } from './sidebar/config-section.js';
import { safetySection } from './sidebar/safety-section.js';
import { artifactsSection } from './sidebar/artifacts-section.js';
import { machinesSection } from './sidebar/machines-section.js';
import { sessionsSection } from './sidebar/sessions-section.js';
import { projectsSection } from './sidebar/projects-section.js';
import { schedulerSection } from './sidebar/scheduler-section.js';
import { councilSection } from './sidebar/council-section.js';
import { servicesSection } from './sidebar/services-section.js';
import { notificationsPanel } from './notifications-panel.js';
import { scratchpad } from './scratchpad.js';
import { armDeadKeySuppressor, disarmDeadKeySuppressor } from './dead-key-suppressor.js';
import { openCommandPalette, isCommandPaletteOpen } from './command-palette.js';
import { setupHelp, openHelp, isHelpOpen } from './help-modal.js';
import { PttController } from './ptt.js';
import { voicePromptWrap } from './voice/prompt.js';
import { isAutoSend } from './voice/autosend-prefs.js';
import { initAnnouncements } from './announcement-modal.js';

// State - track open windows
const sessionWindows = new Map();  // sessionId -> SessionWindow instance
const artifactWindows = new Map();  // artifactId -> ArtifactWindow instance
const reviewWindows = new Map();  // windowId -> ReviewWindow instance
let councilWindow = null;  // single CouncilWindow instance (one board at a time)

// Global PTT state
let globalPttState = 'idle';  // idle | recording | processing (mirrors globalPttCtl)

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
    transcriptBar: document.getElementById('sidebarTranscriptBar'),
    transcriptInput: document.getElementById('sidebarTranscriptInput'),
    transcriptSend: document.getElementById('sidebarTranscriptSend'),
    transcriptDismiss: document.getElementById('sidebarTranscriptDismiss'),
    instantBanner: document.getElementById('instantModeBanner'),
    instantBannerDismiss: document.getElementById('instantModeBannerDismiss'),
};

// Initialize
document.addEventListener('DOMContentLoaded', init);

async function init() {
    sidebar.init();
    document.getElementById('sidebarQuicktask')?.addEventListener('click', (e) => {
        e.stopPropagation();
        openCommandPalette();
    });
    document.getElementById('sidebarNewIdea')?.addEventListener('click', (e) => {
        e.stopPropagation();
        sidebar.close();
        openCommandPalette({ view: 'new-idea' });
    });
    document.getElementById('sidebarHelp')?.addEventListener('click', (e) => {
        e.stopPropagation();
        sidebar.close();
        openHelp();
    });
    sidebar.addSection('sessions', sessionsSection);
    sidebar.addSection('services', servicesSection);
    sidebar.addSection('machines', machinesSection);
    sidebar.addSection('projects', projectsSection);
    sidebar.addSection('artifacts', artifactsSection);
    sidebar.addSection('scheduler', schedulerSection);
    sidebar.addSection('council', councilSection);
    sidebar.addSection('safety', safetySection);
    sidebar.addSection('config', configSection);
    setupClock();
    setupPageUnload();
    setupGlobalPtt();
    setupWindowCycling();
    setupWindowSwipeCycling();
    setupCollage();
    setupHelp();

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

    // Voice tier drives PTT path selection + the instant-mode banner
    desktop.on('voice_status', renderInstantModeBanner);
    await desktop.fetchVoiceStatus();
    setupTranscriptBar();

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
        // MCP-driven focus follows the same "voice follows the tab" rule as a
        // user click: only session windows update the active-session shadow.
        const sw = sessionWindows.get(window_id);
        if (sw) postActiveSession(sw.session);
    });

    desktop.on('desktop_tile_window', ({ window_id, zone }) => {
        tileManager._tileWindow(window_id, zone);
    });

    desktop.on('desktop_minimize_all', () => {
        desktop.minimizeAllExcept(null);
    });

    desktop.on('desktop_collage', () => {
        collage.toggle();
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

    // Scratch pad drawer (Alt+N, right-edge handle, selection capture)
    scratchpad.init();

    // Click on a toast -> open the subject session it's about as interactive terminal.
    // No subject (e.g. a system-level toast) -> no-op, never fall back to the bridge.
    document.addEventListener('open-notification-session', (e) => {
        const session = e.detail && e.detail.session;
        if (session) {
            openSessionTerminal(session, 'terminal');
        }
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

    // After a reboot/portal restart the persisted taskbar still references tmux sessions
    // that no longer exist. Reconcile the restored tabs against the first live sessions
    // list and drop the dead ones. One-shot so transient list hiccups can't kill an
    // active window mid-session.
    desktop.once('sessions', reconcileTaskbarWithSessions);

    // Fetch initial data in the background (will emit events to listeners above)
    desktop.fetchSessions();

    // Show a userbase announcement if there's a new one (non-blocking,
    // fails silently — never holds up or breaks the desktop).
    initAnnouncements();
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
    // Only notify if the session window isn't the one you're looking at.
    if (desktop.getActiveWindow() === session) return;
    // Respects browser permission + the Config sidebar mute toggle.
    // Enabling is done explicitly from Config → Desktop notifications, not here.
    if (!notificationsActive()) return;
    new Notification(`Activity in ${session}`, {
        body: 'Session has new output',
        icon: '/static/img/icon-192.png',
        tag: `activity-${session}`,  // Prevent duplicate notifications
    });
}

// Move focus to the next (+1) / previous (-1) open window, wrapping around.
// Shared by Tab/Shift+Tab and the two-finger swipe gesture.
function cycleWindow(direction) {
    const items = Array.from(elements.taskbarWindows.querySelectorAll('.sidebar-open-item'));
    if (items.length === 0) return;
    const activeId = desktop.getActiveWindow ? desktop.getActiveWindow() : null;
    const currentIndex = items.findIndex(el => el.dataset.session === activeId);
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
}

// Tab / Shift+Tab to cycle open windows
function setupWindowCycling() {
    window.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab') return;
        e.preventDefault();
        e.stopPropagation();
        cycleWindow(e.shiftKey ? -1 : 1);
    }, true);  // capture phase — runs before xterm's handlers
}

// Two-finger horizontal swipe (trackpad) to cycle windows — the gesture analog
// of Tab/Shift+Tab. A horizontal swipe also fires the browser's back/forward
// navigation, so we intercept horizontal-dominant wheel events over the desktop
// (preventing that nav) and translate one deliberate swipe into one cycle.
function setupWindowSwipeCycling() {
    let accum = 0;
    let lastT = 0;
    let cooldown = false;
    const SWIPE_THRESHOLD = 60;   // px of horizontal travel for a deliberate swipe
    const GESTURE_GAP = 200;      // ms gap that starts a fresh gesture
    const COOLDOWN_MS = 500;      // one cycle per swipe; ride out momentum

    window.addEventListener('wheel', (e) => {
        // Horizontal-dominant only — vertical scroll is left untouched.
        if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
        // Let genuinely horizontally-scrollable content (e.g. a wide Monitor
        // pane) scroll itself instead of cycling.
        if (isWithinHorizontalScroller(e.target)) return;
        const items = elements.taskbarWindows.querySelectorAll('.sidebar-open-item');
        if (items.length < 2) return;

        // We own this gesture: stop the back/forward navigation swipe.
        e.preventDefault();

        const now = e.timeStamp;
        if (now - lastT > GESTURE_GAP) accum = 0;  // new gesture
        lastT = now;
        if (cooldown) return;

        accum += e.deltaX;
        if (Math.abs(accum) < SWIPE_THRESHOLD) return;
        // Swipe left (content/fingers move left → +deltaX) → next; right → prev.
        cycleWindow(accum > 0 ? 1 : -1);
        accum = 0;
        cooldown = true;
        setTimeout(() => { cooldown = false; }, COOLDOWN_MS);
    }, { passive: false });

    // Touch devices (tablets on the desktop route — `/` always serves the
    // desktop, there's no mobile redirect) don't fire wheel events, so add the
    // single-finger swipe equivalent. Capture phase so an xterm terminal can't
    // swallow the gesture; sidebar and the command palette opt out.
    const surface = document.getElementById('desktopArea') || document;
    attachHorizontalSwipe(surface, cycleWindow, {
        capture: true,
        ignore: (t) => !!(t.closest && t.closest('.sidebar, .cmdk-overlay')),
    });
}

/** True if the target sits inside an element that can actually scroll sideways. */
function isWithinHorizontalScroller(target) {
    let el = target instanceof Element ? target : null;
    while (el && el !== document.body) {
        if (el.scrollWidth > el.clientWidth + 1) {
            const ox = getComputedStyle(el).overflowX;
            if (ox === 'auto' || ox === 'scroll') return true;
        }
        el = el.parentElement;
    }
    return false;
}

// Collage — tap F3 (like macOS Mission Control) or Alt/Option+` to grid all
// open windows; tap again, press Esc, or click a window to restore.
// Dead-key handling (Option+` composes a grave accent) lives in the shared
// suppressor — see dead-key-suppressor.js.
function setupCollage() {
    collage.init(_lookupWindowInstance);

    // Capture phase on window + stopPropagation so xterm's <textarea> never
    // sees the keystroke (and the browser default — F3 find-next — is
    // suppressed). Alt+` is detected via e.code: on macOS the dead key makes
    // e.key 'Dead', never a backtick. Cmd/Ctrl+` stays the sidebar toggle.
    window.addEventListener('keydown', (e) => {
        const isF3 = e.code === 'F3';
        const isAltBacktick = e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'Backquote';
        if (!isF3 && !isAltBacktick) return;
        if (isCommandPaletteOpen() || isHelpOpen()) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.repeat) return;  // ignore auto-repeat while the key is held
        // Arm BEFORE toggling — the suppressor must be active when the
        // pending composition finalizes (blur of the focused terminal).
        if (isAltBacktick) armDeadKeySuppressor();
        const wasActive = collage.active;
        collage.toggle();
        // No-op toggle (under 2 windows): disarm so the dead key composes
        // normally — with no collage there's nothing to protect.
        if (isAltBacktick && collage.active === wasActive) disarmDeadKeySuppressor();
    }, true);
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

// "Voice follows the focused tab" — mirror the focused session name to the
// portal backend, which writes ~/.agentwire/active-session. External tools
// (Hammerspoon ⌥Space) read that file to know where voice input should land.
// Session windows only; artifacts/panels never change the voice target.
// Fire-and-forget + deduped so re-focusing the same window doesn't spam.
let _lastPostedActiveSession = null;
function postActiveSession(session) {
    if (!session || session === _lastPostedActiveSession) return;
    _lastPostedActiveSession = session;
    apiFetch('/api/active-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session }),
    }).catch(() => { _lastPostedActiveSession = null; });  // allow retry on failure
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
    // Normalize once at the boundary — see session-id.js for the encoding rules.
    machine = normalizeMachine(machine);
    const id = buildSessionId(session, machine);

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
            // Voice follows the focused tab — mirror this session to the
            // active-session shadow file so ⌥Space (Hammerspoon) targets it.
            postActiveSession(session);
            // Tabbing into a session clears its idle/worker toasts — the user
            // has seen it, so leaving the notification up is just noise.
            notificationsPanel.dismissForSession(session);
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

/**
 * Open a mobile-first Review window for a session — diff + tap-to-approve/deny.
 * Re-opening focuses (and refreshes) the existing window for that session.
 *
 * @param {string} session - Session whose diff to review
 */
export function openReviewWindow(session) {
    const id = `review-${session}`;
    if (reviewWindows.has(id)) {
        const existing = reviewWindows.get(id);
        if (existing.isMinimized) {
            if (!desktop.isTiled(id)) desktop.minimizeAllExcept(id);
            existing.restore();
        } else {
            existing.focus();
        }
        existing.refresh();
        return;
    }

    desktop.minimizeAllExcept(null);

    const rw = new ReviewWindow({
        session,
        windowId: id,
        root: elements.desktopArea,
        onClose: () => {
            reviewWindows.delete(id);
            removeTaskbarButton(id);
            unrecordTaskbarEntry(id);
        },
        onFocus: () => {
            updateTaskbarActive(id);
            desktop.setActiveWindow(id);
            saveTaskbarState();
        },
    });

    rw.open();
    reviewWindows.set(id, rw);
    addTaskbarButton(id, rw);
    recordTaskbarEntry({ kind: 'review', id, session });
}

/**
 * Open the council workspace window. Registers through the same desktop path as
 * a session window (single-window maximize + taskbar tab), so it opens
 * maximized, appears in the open-sessions area, and is tabbable. Only one board
 * exists at a time — re-opening focuses (and optionally re-targets) it.
 *
 * @param {string|null} sitting - Sitting to show (defaults to the sole live one)
 */
export function openCouncilWindow(sitting = null) {
    const id = COUNCIL_WINDOW_ID;
    if (councilWindow && councilWindow.isOpen) {
        if (councilWindow.isMinimized) {
            if (!desktop.isTiled(id)) desktop.minimizeAllExcept(id);
            councilWindow.restore();
        } else {
            councilWindow.focus();
        }
        if (sitting) councilWindow.showSitting(sitting);
        return;
    }

    // Minimize all other windows before opening new one
    desktop.minimizeAllExcept(null);

    councilWindow = new CouncilWindow({
        sitting,
        root: elements.desktopArea,
        onClose: () => {
            councilWindow = null;
            removeTaskbarButton(id);
            unrecordTaskbarEntry(id);
        },
        onFocus: () => {
            updateTaskbarActive(id);
            desktop.setActiveWindow(id);
            saveTaskbarState();
        },
    });

    councilWindow.open();
    addTaskbarButton(id, councilWindow);
    recordTaskbarEntry({ kind: 'council', id, sitting });
}

// Taskbar management — persist open windows + order across page refresh
const TASKBAR_STATE_KEY = 'taskbar-state';
const taskbarRecords = new Map(); // id -> { kind, id, ...args }
let taskbarDragoverBound = false;
let restoringTaskbar = false;

function _lookupWindowInstance(id) {
    if (id === COUNCIL_WINDOW_ID && councilWindow) return councilWindow;
    return sessionWindows.get(id) || artifactWindows.get(id) || null;
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
    } else if (rec.kind === 'council') {
        openCouncilWindow(rec.sitting || null);
    } else if (rec.kind === 'review') {
        openReviewWindow(rec.session);
    }
}

export function restoreTaskbarState() {
    const { tabs: rawTabs, activeId } = loadTaskbarState();
    // Phase 3 moved panels to sidebar accordions — filter out stale panel records.
    const tabs = rawTabs.filter(t => t.kind !== 'panel');
    if (tabs.length === 0) return;
    restoringTaskbar = true;
    try {
        // Materialize EVERY saved window — not just the active one — so they all
        // register with the desktop manager. Otherwise only the active window is a
        // real window and the rest are click-to-open placeholders, which leaves
        // collage (F3) and Tab window-cycling with nothing to act on
        // until each other window is clicked. Open in saved order so the sidebar
        // "Open Windows" list keeps its order (open* appends to the end).
        const focusRec = (activeId && tabs.find(t => t.id === activeId)) || tabs[tabs.length - 1];
        for (const rec of tabs) {
            try {
                _openByRecord(rec);
            } catch (e) {
                console.warn('[taskbar] Failed to restore window', rec, e);
            }
        }
        // Each open maximizes itself + minimizes the rest, so whichever opened last
        // is active. Re-activate the saved-active window: maximizes it, minimizes
        // the others — leaving the exact single-window state we had before refresh.
        if (focusRec && desktop.windows.has(focusRec.id)) {
            desktop.setActiveWindow(focusRec.id);
        }
    } finally {
        restoringTaskbar = false;
        saveTaskbarState();
    }
}

/**
 * Drop restored taskbar tabs whose underlying session no longer exists.
 *
 * After a reboot or portal restart, tmux sessions are gone but the persisted
 * taskbar (localStorage) still references them — leaving dead "Open Windows"
 * tabs that never self-correct. We reconcile against the live sessions list.
 *
 * Only local sessions are reconciled: /api/sessions/local is authoritative and
 * arrives first, whereas remote sessions merge in asynchronously and must not be
 * pruned on the local-only snapshot. Artifact tabs are file-backed, not
 * session-backed, so they survive a reboot and are left alone.
 *
 * @param {Array<{name: string, machine?: string}>} sessions - Live sessions list
 */
function reconcileTaskbarWithSessions(sessions) {
    const liveLocalIds = new Set(
        (sessions || [])
            .filter(s => isLocalMachine(s.machine))
            .map(s => buildSessionId(s.name, null))
    );
    const buttons = Array.from(elements.taskbarWindows.querySelectorAll('.sidebar-open-item'));
    for (const btn of buttons) {
        const id = btn.dataset.session;
        const rec = taskbarRecords.get(id);
        if (!rec || rec.kind !== 'session') continue;
        if (!isLocalMachine(rec.machine)) continue;  // remote: not authoritative yet
        if (liveLocalIds.has(id)) continue;           // session still alive

        // Stale tab. A materialized window cleans up its own button + record via
        // onClose; a placeholder has no instance, so tear it down directly.
        const inst = _lookupWindowInstance(id);
        if (inst && typeof inst.close === 'function') {
            try { inst.close(); } catch (e) {}
        } else {
            btn.remove();
            unrecordTaskbarEntry(id);
        }
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
        // Cmd/Ctrl + K opens the command palette on the root list, with
        // "Ask council" as the default selection. xterm.js uses a hidden
        // textarea for terminal input, so we don't skip on tag — Cmd+K is
        // always intercepted.
        if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            if (!isCommandPaletteOpen() && !isHelpOpen()) openCommandPalette();
        }
    });
    document.addEventListener('keyup', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.code === 'Space' && globalPttState === 'recording') {
            e.preventDefault();
            stopGlobalRecording();
        }
    });
}

const globalPttCtl = new PttController({
    getVoiceStatus: () => desktop.voiceStatus,
    onState: updateGlobalPttState,
    onResult: (text) => {
        if (isAutoSend()) sendGlobalVoiceText(text);
        else showGlobalTranscript(text);
    },
    onError: (kind, message) => {
        if (kind === 'unsupported') {
            console.warn('[GlobalPTT] SpeechRecognition unsupported — use Chrome or set stt.backend: cloud/custom');
            const icon = elements.globalPtt?.querySelector('.ptt-icon');
            if (icon) icon.textContent = '🚫';
            elements.globalPtt.title = message;
            return;
        }
        console.warn(`[GlobalPTT] ${message}`);
    },
});

function startGlobalRecording() {
    globalPttCtl.start();
}

function stopGlobalRecording() {
    globalPttCtl.stop();
}

async function sendGlobalVoiceText(text) {
    try {
        await apiFetch('/send/agentwire', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: voicePromptWrap(text) })
        });
    } catch (err) {
        console.error('[GlobalPTT] Send failed:', err);
    }
}

// --- Edit-before-send transcript bar ---

function setupTranscriptBar() {
    const { transcriptBar, transcriptInput, transcriptSend, transcriptDismiss } = elements;
    if (!transcriptBar) return;

    const send = () => {
        const value = transcriptInput.value.trim();
        hideGlobalTranscript();
        if (value) sendGlobalVoiceText(value);
    };
    transcriptSend.addEventListener('click', send);
    transcriptDismiss.addEventListener('click', hideGlobalTranscript);
    transcriptInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); send(); }
        else if (e.key === 'Escape') { e.preventDefault(); hideGlobalTranscript(); }
        e.stopPropagation();
    });
}

function showGlobalTranscript(text) {
    const { transcriptBar, transcriptInput } = elements;
    if (!transcriptBar) return;
    transcriptBar.hidden = false;
    transcriptInput.value = text;
    transcriptInput.focus();
    transcriptInput.select();
}

function hideGlobalTranscript() {
    if (elements.transcriptBar) elements.transcriptBar.hidden = true;
}

// --- Instant-mode banner ---

const INSTANT_BANNER_DISMISSED_KEY = 'aw-instant-banner-dismissed';

function renderInstantModeBanner(status) {
    const banner = elements.instantBanner;
    if (!banner) return;
    const dismissed = localStorage.getItem(INSTANT_BANNER_DISMISSED_KEY) === '1';
    banner.hidden = !(status?.instant_mode && !dismissed);
    if (!banner.dataset.wired) {
        banner.dataset.wired = '1';
        elements.instantBannerDismiss?.addEventListener('click', () => {
            localStorage.setItem(INSTANT_BANNER_DISMISSED_KEY, '1');
            banner.hidden = true;
        });
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
