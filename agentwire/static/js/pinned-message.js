/**
 * Pinned Message Panel — floating, draggable, resizable panel that displays a
 * pinned message body. Anchored to document.body so it survives switching
 * between conversation windows / sessions.
 *
 * v1 scope: one pinned panel at a time. Pinning a new message replaces the
 * existing one. State is in-memory only (per portal session).
 */

const MIN_WIDTH = 200;
const MIN_HEIGHT = 140;
const DEFAULT_WIDTH = 320;
const DEFAULT_HEIGHT = 220;
const EDGE_PADDING = 8;

class PinnedMessagePanel {
    constructor() {
        this.panel = null;
        this.headerEl = null;
        this.bodyEl = null;
        this.resizeHandleEl = null;
        this.roleLabelEl = null;
        this.unpinBtnEl = null;

        // Current pinned message ({ role, text, html })
        this.current = null;

        // Geometry (px). Initial null -> placed near top-right on first pin.
        this.x = null;
        this.y = null;
        this.width = DEFAULT_WIDTH;
        this.height = DEFAULT_HEIGHT;

        // Drag state
        this._dragging = false;
        this._dragOffsetX = 0;
        this._dragOffsetY = 0;
        // Resize state
        this._resizing = false;
        this._resizeStartX = 0;
        this._resizeStartY = 0;
        this._resizeStartWidth = 0;
        this._resizeStartHeight = 0;

        this._onPointerMove = this._onPointerMove.bind(this);
        this._onPointerUp = this._onPointerUp.bind(this);
        this._onViewportResize = this._onViewportResize.bind(this);
    }

    /**
     * Pin a message. If a panel is already shown, replaces its content
     * while keeping position/size.
     * @param {{ role?: string, text?: string, html?: string }} message
     */
    pin(message) {
        if (!message) return;
        this.current = {
            role: message.role || 'message',
            text: message.text || '',
            html: message.html || null,
        };

        if (!this.panel) {
            this._createPanel();
            this._placeInitial();
        }

        this._renderContent();
        this.panel.style.display = 'flex';
        this._applyGeometry();
    }

    /**
     * Unpin and remove the panel from the DOM.
     */
    unpin() {
        if (!this.panel) return;
        this.panel.remove();
        this.panel = null;
        this.headerEl = null;
        this.bodyEl = null;
        this.resizeHandleEl = null;
        this.roleLabelEl = null;
        this.unpinBtnEl = null;
        this.current = null;
        window.removeEventListener('resize', this._onViewportResize);
    }

    /**
     * True if a message is currently pinned.
     */
    isPinned() {
        return this.current !== null;
    }

    _createPanel() {
        const panel = document.createElement('div');
        panel.className = 'pinned-message-panel';
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-label', 'Pinned message');
        panel.innerHTML = `
            <div class="pinned-message-header" data-drag-handle>
                <span class="pinned-message-role"></span>
                <span class="pinned-message-title">Pinned</span>
                <button class="pinned-message-unpin" title="Unpin (remove)" aria-label="Unpin">×</button>
            </div>
            <div class="pinned-message-body"></div>
            <div class="pinned-message-resize-handle" title="Drag to resize" aria-hidden="true"></div>
        `;

        this.panel = panel;
        this.headerEl = panel.querySelector('.pinned-message-header');
        this.bodyEl = panel.querySelector('.pinned-message-body');
        this.resizeHandleEl = panel.querySelector('.pinned-message-resize-handle');
        this.roleLabelEl = panel.querySelector('.pinned-message-role');
        this.unpinBtnEl = panel.querySelector('.pinned-message-unpin');

        this.unpinBtnEl.addEventListener('click', (e) => {
            e.stopPropagation();
            this.unpin();
        });

        this.headerEl.addEventListener('pointerdown', (e) => this._onDragStart(e));
        this.resizeHandleEl.addEventListener('pointerdown', (e) => this._onResizeStart(e));

        window.addEventListener('resize', this._onViewportResize);

        document.body.appendChild(panel);
    }

    _renderContent() {
        if (!this.current) return;
        this.roleLabelEl.textContent = this.current.role || 'message';
        this.roleLabelEl.dataset.role = this.current.role || 'message';

        if (this.current.html) {
            this.bodyEl.innerHTML = this.current.html;
        } else {
            this.bodyEl.textContent = this.current.text || '';
        }
    }

    _placeInitial() {
        if (this.x !== null && this.y !== null) return;
        const viewportW = window.innerWidth;
        const viewportH = window.innerHeight;
        // Default position: upper-right inside the viewport.
        this.x = Math.max(EDGE_PADDING, viewportW - this.width - 24);
        this.y = 24;
        this._clampGeometry(viewportW, viewportH);
    }

    _applyGeometry() {
        if (!this.panel) return;
        this.panel.style.left = `${this.x}px`;
        this.panel.style.top = `${this.y}px`;
        this.panel.style.width = `${this.width}px`;
        this.panel.style.height = `${this.height}px`;
    }

    _clampGeometry(viewportW, viewportH) {
        const w = viewportW ?? window.innerWidth;
        const h = viewportH ?? window.innerHeight;
        this.width = Math.max(MIN_WIDTH, Math.min(this.width, w - EDGE_PADDING * 2));
        this.height = Math.max(MIN_HEIGHT, Math.min(this.height, h - EDGE_PADDING * 2));
        this.x = Math.max(EDGE_PADDING, Math.min(this.x, w - this.width - EDGE_PADDING));
        this.y = Math.max(EDGE_PADDING, Math.min(this.y, h - this.height - EDGE_PADDING));
    }

    _onDragStart(e) {
        // Ignore drags that start on the unpin button or other interactive children
        if (e.target.closest('.pinned-message-unpin')) return;
        if (!this.panel) return;
        this._dragging = true;
        this._dragOffsetX = e.clientX - this.x;
        this._dragOffsetY = e.clientY - this.y;
        this.panel.classList.add('dragging');
        this.headerEl.setPointerCapture?.(e.pointerId);
        document.addEventListener('pointermove', this._onPointerMove);
        document.addEventListener('pointerup', this._onPointerUp);
        e.preventDefault();
    }

    _onResizeStart(e) {
        if (!this.panel) return;
        this._resizing = true;
        this._resizeStartX = e.clientX;
        this._resizeStartY = e.clientY;
        this._resizeStartWidth = this.width;
        this._resizeStartHeight = this.height;
        this.panel.classList.add('resizing');
        this.resizeHandleEl.setPointerCapture?.(e.pointerId);
        document.addEventListener('pointermove', this._onPointerMove);
        document.addEventListener('pointerup', this._onPointerUp);
        e.preventDefault();
        e.stopPropagation();
    }

    _onPointerMove(e) {
        if (this._dragging) {
            this.x = e.clientX - this._dragOffsetX;
            this.y = e.clientY - this._dragOffsetY;
            this._clampGeometry();
            this._applyGeometry();
        } else if (this._resizing) {
            const dx = e.clientX - this._resizeStartX;
            const dy = e.clientY - this._resizeStartY;
            this.width = this._resizeStartWidth + dx;
            this.height = this._resizeStartHeight + dy;
            this._clampGeometry();
            this._applyGeometry();
        }
    }

    _onPointerUp() {
        if (this._dragging || this._resizing) {
            this._dragging = false;
            this._resizing = false;
            this.panel?.classList.remove('dragging', 'resizing');
            document.removeEventListener('pointermove', this._onPointerMove);
            document.removeEventListener('pointerup', this._onPointerUp);
        }
    }

    _onViewportResize() {
        if (!this.panel) return;
        this._clampGeometry();
        this._applyGeometry();
    }
}

export const pinnedMessage = new PinnedMessagePanel();
