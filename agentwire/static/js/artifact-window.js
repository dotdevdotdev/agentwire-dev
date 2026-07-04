/**
 * artifact-window.js
 *
 * ArtifactWindow class — displays agent-generated HTML or external URLs
 * in a sandboxed iframe within a WinBox window.
 */

import { apiFetch } from './api.js';
import { desktop } from './desktop-manager.js';
import { toast } from './toast.js';

// If the iframe's `load` event hasn't fired by now, treat it as stuck and flip
// the spinner to the error state. The native `error` event doesn't fire for
// many failure shapes (blank response, hung connection), so the spinner would
// otherwise spin forever (issue #17).
const ARTIFACT_LOAD_TIMEOUT_MS = 15000;

export class ArtifactWindow {
    /**
     * @param {Object} options
     * @param {string} options.url - URL to load (relative /artifacts/... or absolute https://...)
     * @param {string} options.title - Window title
     * @param {string} options.artifactId - Unique window identifier
     * @param {HTMLElement} options.root - Parent element for WinBox
     * @param {Function} options.onClose - Callback when window closes
     * @param {Function} options.onFocus - Callback when window gains focus
     */
    constructor(options) {
        this.url = options.url;
        this.title = options.title || 'Artifact';
        this.artifactId = options.artifactId;
        this.root = options.root || document.body;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;

        this.winbox = null;
        this.iframe = null;
        this.isOpen = false;
        this._loadTimer = null; // stuck-spinner watchdog
    }

    /**
     * Open the artifact window.
     */
    open() {
        if (this.isOpen) {
            this.focus();
            return;
        }

        const container = this._createContainer();
        this._createWinBox(container);
        this._loadUrl();
        this.isOpen = true;
    }

    /**
     * Close the artifact window and clean up.
     */
    close() {
        if (!this.isOpen) return;

        this._clearLoadTimeout();

        // Remove iframe to stop any running scripts
        if (this.iframe) {
            this.iframe.removeAttribute('srcdoc');
            this.iframe.src = 'about:blank';
            this.iframe = null;
        }

        if (this.winbox) {
            const wb = this.winbox;
            this.winbox = null;
            wb.close();
        }

        desktop.unregisterWindow(this.artifactId);
        this.isOpen = false;

        if (this.onCloseCallback) {
            this.onCloseCallback(this);
        }
    }

    focus() {
        if (this.winbox) this.winbox.focus();
    }

    minimize() {
        if (this.winbox) this.winbox.minimize();
    }

    restore() {
        if (this.winbox) this.winbox.restore();
    }

    get isMinimized() {
        return this.winbox ? this.winbox.min : false;
    }

    /**
     * Reload the iframe content.
     */
    reload() {
        if (this.iframe) {
            this._resetLoadingUI();
            this._armLoadTimeout();
            this._setIframeSrc();
        }
    }

    // Private methods

    _content() {
        return this.winbox ? this.winbox.body.querySelector('.artifact-window-content') : null;
    }

    _clearLoadTimeout() {
        if (this._loadTimer) {
            clearTimeout(this._loadTimer);
            this._loadTimer = null;
        }
    }

    _armLoadTimeout() {
        this._clearLoadTimeout();
        this._loadTimer = setTimeout(() => {
            this._loadTimer = null;
            this._showLoadError(`Timed out loading: ${this.url}`);
        }, ARTIFACT_LOAD_TIMEOUT_MS);
    }

    _resetLoadingUI() {
        const content = this._content();
        if (!content) return;
        content.querySelector('.artifact-loading')?.classList.remove('hidden');
        content.querySelector('.artifact-error')?.classList.add('hidden');
    }

    _showLoadError(message) {
        this._clearLoadTimeout();
        const content = this._content();
        if (!content) return;
        content.querySelector('.artifact-loading')?.classList.add('hidden');
        const errorEl = content.querySelector('.artifact-error');
        if (errorEl) {
            errorEl.classList.remove('hidden');
            const msgEl = errorEl.querySelector('.artifact-error-message');
            if (msgEl) msgEl.textContent = message;
        }
    }

    _createContainer() {
        const container = document.createElement('div');
        container.className = 'artifact-window-content';
        container.innerHTML = `
            <div class="artifact-loading">Loading...</div>
            <div class="artifact-error hidden">
                <div class="artifact-error-message">Failed to load</div>
                <button class="btn btn-primary artifact-reload-btn">Reload</button>
            </div>
        `;
        return container;
    }

    _createWinBox(container) {
        this.winbox = new WinBox({
            title: this.title,
            icon: '<span style="font-size:14px">&#x1F4CB;</span>',
            mount: container,
            root: this.root,
            width: '80%',
            height: '80%',
            x: 'center',
            y: 'center',
            minwidth: 320,
            minheight: 240,
            class: ['artifact-window'],
            onclose: () => {
                this.winbox = null;
                this.close();
                return false;
            },
            onfocus: () => {
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
            onminimize: () => {
                desktop.emit('window_minimized', { id: this.artifactId });
            },
            onrestore: () => {
                desktop.emit('window_restored', { id: this.artifactId });
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
        });

        desktop.registerWindow(this.artifactId, this.winbox);

        // Set up reload button
        const reloadBtn = container.querySelector('.artifact-reload-btn');
        if (reloadBtn) {
            reloadBtn.addEventListener('click', () => this.reload());
        }

        this._setupDownloadButton();
    }

    /**
     * Titlebar download button — local artifacts only (external URLs have
     * nothing on our disk to serve). Fetches via apiFetch so the bearer token
     * rides along (a plain <a href> navigation can't carry the Authorization
     * header), then saves the blob. This is a separate top-level fetch — the
     * iframe srcdoc/sandbox model (#645) is untouched.
     */
    _setupDownloadButton() {
        if (this._isExternalUrl()) return;
        const relPath = this._artifactRelPath();
        if (!relPath) return;
        const titleEl = this.winbox.window.querySelector('.wb-title');
        if (!titleEl) return;

        const btn = document.createElement('button');
        btn.className = 'wb-title-download';
        btn.title = 'Download HTML';
        btn.innerHTML = '<span aria-hidden="true">&#x2B73;</span>';
        titleEl.insertBefore(btn, titleEl.firstChild);

        // WinBox attaches capture-phase mousedown on .wb-drag for window
        // dragging — stop it at pointerdown so a click doesn't start a drag.
        btn.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            e.stopPropagation();
        }, true);
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._downloadArtifact(relPath);
        });
    }

    /** Relative path under the artifacts root, or null if not an /artifacts/ URL. */
    _artifactRelPath() {
        const resolved = this._resolveUrl();
        if (!resolved.startsWith('/artifacts/')) return null;
        const rel = resolved.slice('/artifacts/'.length).split('?')[0].split('#')[0];
        return rel || null;
    }

    async _downloadArtifact(relPath) {
        const encoded = relPath.split('/').map(encodeURIComponent).join('/');
        try {
            const resp = await apiFetch(`/api/artifacts/download/${encoded}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const blob = await resp.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = relPath.split('/').pop();
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(a.href);
        } catch (e) {
            toast(`Download failed: ${e.message}`, { type: 'error' });
        }
    }

    _resolveUrl() {
        const url = this.url;
        // Absolute URLs (http/https) — use as-is
        if (url.startsWith('http://') || url.startsWith('https://')) {
            return url;
        }
        // Already a path starting with /
        if (url.startsWith('/')) {
            return url;
        }
        // Relative filename — serve from /artifacts/
        return `/artifacts/${url}`;
    }

    _isExternalUrl() {
        return this.url.startsWith('http://') || this.url.startsWith('https://');
    }

    /**
     * Point the iframe at the artifact. Plain iframe GETs can't carry the
     * Authorization header, so local artifacts are fetched with credentials
     * (apiFetch) and rendered via `srcdoc`. Under `sandbox="allow-scripts"`
     * (no allow-same-origin) the srcdoc document gets a null/opaque origin:
     * its scripts run, but it cannot read the portal's localStorage (bearer
     * token) or make authenticated same-origin API calls. Artifacts are
     * agent-written arbitrary HTML/JS — this isolation is load-bearing.
     * Note: relative sub-resources inside multi-file artifacts won't resolve
     * under srcdoc — self-contained HTML is the supported shape there.
     */
    async _setIframeSrc() {
        const resolved = this._resolveUrl();
        if (this._isExternalUrl()) {
            this.iframe.src = resolved;
            return;
        }
        try {
            const resp = await apiFetch(resolved);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            this.iframe.srcdoc = await resp.text();
        } catch (e) {
            this._showLoadError(`Failed to load: ${this.url}`);
        }
    }

    _loadUrl() {
        if (!this.winbox) return;

        // Find the .artifact-window-content container (mounted inside .wb-body)
        const content = this.winbox.body.querySelector('.artifact-window-content');
        if (!content) return;

        const loadingEl = content.querySelector('.artifact-loading');
        const errorEl = content.querySelector('.artifact-error');

        // Create iframe with appropriate sandbox
        this.iframe = document.createElement('iframe');
        this.iframe.className = 'artifact-iframe';

        // Sandboxing:
        // - Local artifacts: allow-scripts ONLY. SECURITY: never add
        //   allow-same-origin here — local artifacts are agent-generated HTML
        //   rendered via srcdoc, and allow-same-origin would run their scripts
        //   in the portal origin, letting a poisoned artifact steal the bearer
        //   token from localStorage and drive every authenticated portal API.
        // - External URLs: allow-scripts allow-forms allow-popups (real origin,
        //   but cross-origin to the portal).
        if (this._isExternalUrl()) {
            this.iframe.sandbox = 'allow-scripts allow-forms allow-popups';
        } else {
            this.iframe.sandbox = 'allow-scripts';
        }

        this.iframe.addEventListener('load', () => {
            this._clearLoadTimeout();
            if (loadingEl) loadingEl.classList.add('hidden');
            if (errorEl) errorEl.classList.add('hidden');
        });

        this.iframe.addEventListener('error', () => {
            this._showLoadError(`Failed to load: ${this.url}`);
        });

        this._armLoadTimeout();
        this._setIframeSrc();
        content.insertBefore(this.iframe, content.firstChild);
    }
}
