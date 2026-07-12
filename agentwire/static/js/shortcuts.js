/**
 * Keyboard-shortcut + feature registry — the single source of truth for the
 * F1 help modal (help-modal.js), the command-palette "help" entry, and the
 * `title=` hints we show on buttons.
 *
 * This module holds the *catalogue*, not the bindings. Each component still
 * registers its own capture-phase keydown listener (there is no central
 * dispatcher — see desktop.js setupCollage / setupGlobalPtt etc.). When you
 * add or change a real binding, update its row here too: the `where` field
 * names the handler so the two stay findable together.
 *
 * Combos are token arrays. `Mod` renders ⌘ on macOS, Ctrl elsewhere; `Alt`
 * renders ⌥ on macOS, Alt elsewhere. Plain strings render verbatim.
 */

const IS_MAC = typeof navigator !== 'undefined'
    && /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent || '');

/** Render one combo token for display. */
function renderToken(tok) {
    if (tok === 'Mod') return IS_MAC ? '⌘' : 'Ctrl';
    if (tok === 'Alt') return IS_MAC ? '⌥' : 'Alt';
    return tok;
}

/** A combo (array of tokens) → array of display strings, for <kbd> rendering. */
export function comboKeys(combo) {
    return combo.map(renderToken);
}

// ---------------------------------------------------------------------------
// Shortcut catalogue — grouped for the help modal.
// where: the file:function that owns the real binding (keep in sync on change).
// ---------------------------------------------------------------------------

export const SHORTCUT_GROUPS = [
    {
        title: 'Global',
        items: [
            { combo: ['Mod', 'K'], desc: 'Open the command palette', where: 'desktop.js setupCommandPalette' },
            { combo: ['F1'], alt: ['?'], desc: 'Show this help', where: 'help-modal.js setupHelp' },
            { combo: ['Alt', '`'], desc: 'Toggle the sidebar', where: 'sidebar.js' },
            { combo: ['Esc'], desc: 'Close the open modal, palette, collage, or sidebar', where: 'various' },
        ],
    },
    {
        title: 'Voice',
        items: [
            { combo: ['Mod', 'Space'], hold: true, desc: 'Push-to-talk — hold to speak to AgentWire (or the focused session)', where: 'desktop.js setupGlobalPtt / session-window.js' },
        ],
    },
    {
        title: 'Windows',
        items: [
            { combo: ['Alt', ']'], alt: ['Alt', '['], desc: 'Cycle to the next / previous window (Tab always goes to the terminal)', where: 'desktop.js setupWindowCycling' },
            { combo: ['F3'], desc: 'Window collage — Mission-Control grid of all windows', where: 'desktop.js setupCollage' },
        ],
    },
    {
        title: 'Panels',
        items: [
            { combo: ['Alt', 'N'], desc: 'Toggle the scratchpad drawer', where: 'scratchpad.js' },
            { combo: ['Alt', 'T'], desc: 'Toggle the Session HUD', where: 'session-hud.js' },
        ],
    },
    {
        title: 'Command palette (while open)',
        items: [
            { combo: ['↑'], alt: ['↓'], desc: 'Move through the list', where: 'command-palette.js' },
            { combo: ['Enter'], desc: 'Run the selected item', where: 'command-palette.js' },
            { combo: ['Esc'], desc: 'Go back, or close the palette', where: 'command-palette.js' },
        ],
    },
];

// ---------------------------------------------------------------------------
// Feature tour — the "you may not know this exists" half. Each row teaches a
// capability and (where relevant) how to reach it.
// ---------------------------------------------------------------------------

export const FEATURE_TOUR = [
    { icon: '🏛', name: 'Council', desc: 'Ask one question, get six lenses (brain, conscience, …) deliberating in parallel. Command palette → Ask council.' },
    { icon: '🎙', name: 'Push-to-talk', desc: 'Hold Ctrl/⌘+Space to talk to a session. Or open /mobile to push-to-talk from your phone.' },
    { icon: '▦', name: 'Window collage', desc: 'F3 fans every window into a grid — click one to dive in. Great for juggling many sessions.' },
    { icon: '💡', name: 'New idea', desc: 'Capture a thought and AgentWire spins up a project with an agent already working on it.' },
    { icon: '⎇', name: 'Worktrees', desc: 'Branch a session into an isolated git worktree for parallel work, from the palette or the Sessions panel.' },
    { icon: '📋', name: 'Scratchpad', desc: 'Alt+N opens a quick notes drawer that travels with you across sessions.' },
    { icon: '⏰', name: 'Scheduler', desc: 'Run recurring tasks on a cron — see and trigger them from the Scheduler panel.' },
    { icon: '🧩', name: 'Artifacts', desc: 'Agents can render live HTML into artifact windows — dashboards, handoffs, reports.' },
    { icon: '🛡', name: 'Safety', desc: 'Damage-control rules block dangerous commands. Review and tune them in the Safety panel.' },
    { icon: '📡', name: 'Machines', desc: 'Attach remote machines and tunnels — run sessions on a GPU box or another host.' },
];
