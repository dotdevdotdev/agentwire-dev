/**
 * Help modal — the F1 / "?" keyboard-shortcut cheat sheet and feature tour.
 *
 * Renders from the shortcuts.js registry (the single source of truth), mirrors
 * the .modal-overlay > .modal pattern from announcement-modal.js, and wires the
 * global triggers (F1, ?). The visible "?" button in the sidebar header and a
 * command-palette entry call openHelp() directly.
 */

import { SHORTCUT_GROUPS, FEATURE_TOUR, comboKeys } from './shortcuts.js';

let modalEl = null;
let escHandler = null;

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

export function isHelpOpen() {
    return modalEl !== null;
}

/** One combo → "<kbd>⌘</kbd><kbd>K</kbd>". */
function renderCombo(combo) {
    return comboKeys(combo).map((k) => `<kbd>${escapeHtml(k)}</kbd>`).join('');
}

/** A shortcut row: primary combo, optional "or" alt combo, optional (hold), desc. */
function renderShortcutRow(item) {
    const primary = renderCombo(item.combo);
    const alt = item.alt ? `<span class="help-or">or</span>${renderCombo(item.alt)}` : '';
    const hold = item.hold ? '<span class="help-hold">hold</span>' : '';
    return `<div class="help-row">
        <div class="help-keys">${primary}${alt}${hold}</div>
        <div class="help-desc">${escapeHtml(item.desc)}</div>
    </div>`;
}

function renderShortcutGroup(group) {
    return `<div class="help-group">
        <h3 class="help-group-title">${escapeHtml(group.title)}</h3>
        ${group.items.map(renderShortcutRow).join('')}
    </div>`;
}

function renderFeature(f) {
    return `<div class="help-feature">
        <span class="help-feature-icon">${escapeHtml(f.icon)}</span>
        <div class="help-feature-text">
            <span class="help-feature-name">${escapeHtml(f.name)}</span>
            <span class="help-feature-desc">${escapeHtml(f.desc)}</span>
        </div>
    </div>`;
}

function renderModal() {
    return `<div class="modal-overlay help-overlay" id="helpOverlay">
        <div class="modal help-modal" role="dialog" aria-label="Keyboard shortcuts and features" aria-modal="true">
            <button class="help-dismiss" data-action="dismiss" title="Close (Esc)" aria-label="Close help">✕</button>
            <h2 class="help-title">Keyboard shortcuts &amp; features</h2>
            <div class="help-content">
                <div class="help-shortcuts">
                    ${SHORTCUT_GROUPS.map(renderShortcutGroup).join('')}
                </div>
                <div class="help-features">
                    <h3 class="help-group-title">Features worth knowing</h3>
                    ${FEATURE_TOUR.map(renderFeature).join('')}
                </div>
            </div>
            <div class="help-footer">
                Press <kbd>F1</kbd> or <kbd>?</kbd> anytime · <kbd>Esc</kbd> to close
            </div>
        </div>
    </div>`;
}

export function closeHelp() {
    if (escHandler) { document.removeEventListener('keydown', escHandler, true); escHandler = null; }
    modalEl?.remove();
    modalEl = null;
}

export function openHelp() {
    if (modalEl) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderModal();
    modalEl = wrapper.firstElementChild;
    document.body.appendChild(modalEl);

    modalEl.addEventListener('click', (e) => {
        if (e.target === modalEl || e.target.closest('[data-action="dismiss"]')) {
            closeHelp();
        }
    });
    // Capture phase so Esc closes the help before xterm / other handlers see it.
    escHandler = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeHelp(); }
    };
    document.addEventListener('keydown', escHandler, true);
}

export function toggleHelp() {
    if (modalEl) closeHelp(); else openHelp();
}

/** True when the event target is a text-editing surface (so "?" types normally). */
function isEditableTarget(t) {
    if (!t) return false;
    const tag = t.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable;
}

/**
 * Wire the global F1 / "?" triggers. Capture phase + preventDefault so F1 never
 * opens the browser's own help, and so the keystroke beats xterm. "?" is
 * ignored while typing in an input/textarea/terminal so it can still be typed.
 */
export function setupHelp() {
    window.addEventListener('keydown', (e) => {
        const isF1 = e.code === 'F1';
        // "?" is Shift+/ — accept the produced character, no modifiers beyond Shift.
        const isQuestion = e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey;
        if (!isF1 && !isQuestion) return;
        // Let people actually type "?" into inputs and terminals.
        if (isQuestion && isEditableTarget(e.target)) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.repeat) return;
        toggleHelp();
    }, true);
}
