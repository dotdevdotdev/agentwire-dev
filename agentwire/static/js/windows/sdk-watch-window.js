/**
 * sdk-watch-window.js
 *
 * Live watcher for an `agentwire repl` session. Connects to
 * `/ws/sdk-watch/<name>`, renders the JSONL event stream as the Textual
 * REPL would: text, thinking, tool_use, tool_result, turn_end.
 *
 * Pure read-only. No input field — close the window to stop tailing.
 *
 * Phase 3 of docs/missions/agentwire-sdk-primitives.md.
 */

import { desktop } from '../desktop-manager.js';

export class SdkWatchWindow {
    constructor(options) {
        this.session = options.session;
        this.windowId = options.windowId || `sdk-watch-${this.session}`;
        this.root = options.root || document.body;
        this.onCloseCallback = options.onClose || null;
        this.onFocusCallback = options.onFocus || null;

        this.winbox = null;
        this.body = null;
        this.ws = null;
        this.isOpen = false;
        this._toolCalls = new Map();    // tool_use_id → {name, input}
    }

    open() {
        if (this.isOpen) {
            this.focus();
            return;
        }
        const container = document.createElement('div');
        container.className = 'sdk-watch-content';
        container.innerHTML = `
            <div class="sdk-watch-status">connecting…</div>
            <div class="sdk-watch-stream"></div>
        `;
        this.body = container;
        this._createWinBox(container);
        this._connect();
        this.isOpen = true;
    }

    close() {
        if (!this.isOpen) return;
        if (this.ws) {
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }
        if (this.winbox) {
            const wb = this.winbox;
            this.winbox = null;
            wb.close();
        }
        desktop.unregisterWindow(this.windowId);
        this.isOpen = false;
        if (this.onCloseCallback) this.onCloseCallback(this);
    }

    focus() { if (this.winbox) this.winbox.focus(); }
    minimize() { if (this.winbox) this.winbox.minimize(); }
    restore() { if (this.winbox) this.winbox.restore(); }
    get isMinimized() { return this.winbox ? this.winbox.min : false; }

    _createWinBox(container) {
        this.winbox = new WinBox({
            title: `watch · ${this.session}`,
            icon: '<span style="font-size:14px">&#x1F441;</span>',
            mount: container,
            root: this.root,
            width: '60%',
            height: '70%',
            x: 'center',
            y: 'center',
            minwidth: 320,
            minheight: 240,
            class: ['sdk-watch-window'],
            onclose: () => {
                this.winbox = null;
                this.close();
                return false;
            },
            onfocus: () => {
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
            onminimize: () => desktop.emit('window_minimized', { id: this.windowId }),
            onrestore: () => {
                desktop.emit('window_restored', { id: this.windowId });
                if (this.onFocusCallback) this.onFocusCallback(this);
            },
        });
        desktop.registerWindow(this.windowId, this.winbox);
    }

    _connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws/sdk-watch/${encodeURIComponent(this.session)}`;
        this.ws = new WebSocket(url);
        this.ws.onopen = () => this._setStatus('connected');
        this.ws.onclose = () => this._setStatus('disconnected');
        this.ws.onerror = () => this._setStatus('error');
        this.ws.onmessage = (msg) => {
            try {
                const ev = JSON.parse(msg.data);
                this._renderEvent(ev);
            } catch (e) {
                // ignore malformed
            }
        };
    }

    _setStatus(text) {
        const el = this.body?.querySelector('.sdk-watch-status');
        if (el) el.textContent = text;
    }

    _stream() {
        return this.body?.querySelector('.sdk-watch-stream') || null;
    }

    _append(html) {
        const s = this._stream();
        if (!s) return;
        const div = document.createElement('div');
        div.className = 'sdk-watch-event';
        div.innerHTML = html;
        s.appendChild(div);
        s.scrollTop = s.scrollHeight;
    }

    _renderEvent(ev) {
        const t = ev.type;
        if (t === 'session') {
            this._append(`<span class="ev-meta">[session ${esc(ev.session_id || '')}]</span>`);
        } else if (t === 'agent_start') {
            const model = ev.model || ev.agent || '';
            this._append(`<span class="ev-meta">[agent_start · ${esc(model)}]</span>`);
        } else if (t === 'agent_end') {
            const ms = ev.duration_ms != null ? ` · ${(ev.duration_ms / 1000).toFixed(1)}s` : '';
            this._append(`<span class="ev-done">[agent_end${ms}]</span>`);
        } else if (t === 'turn_end') {
            const u = ev.usage || {};
            const totalTok = (u.input || 0) + (u.output || 0);
            const cost = u.cost?.total_usd ?? u.cost?.total ?? null;
            const parts = [];
            if (totalTok) parts.push(`${totalTok} tok`);
            if (cost != null) parts.push(`$${Number(cost).toFixed(4)}`);
            const suffix = parts.length ? ' · ' + parts.join(' · ') : '';
            this._append(`<span class="ev-done">[turn_end${suffix}]</span>`);
        } else if (t === 'message_end') {
            this._renderMessage(ev.message || {});
        } else if (t === 'user_input') {
            this._append(`<span class="ev-user">› ${esc(ev.text || '')}</span>`);
        } else if (t === 'restart') {
            this._append(`<span class="ev-meta">[restart]</span>`);
        } else if (t === 'error') {
            this._append(`<span class="ev-error">[error: ${esc(ev.error || '')}]</span>`);
        }
    }

    _renderMessage(message) {
        const role = message.role || '';
        const content = Array.isArray(message.content) ? message.content : [];
        for (const block of content) {
            const bt = block?.type;
            if (bt === 'text') {
                if (role === 'assistant') {
                    this._append(`<span class="ev-text">${esc(block.text || '')}</span>`);
                } else {
                    this._append(`<span class="ev-user">${esc(block.text || '')}</span>`);
                }
            } else if (bt === 'thinking') {
                const first = String(block.thinking || '').split('\n', 2)[0].trim();
                if (first) {
                    const preview = first.length <= 80 ? first : first.slice(0, 77) + '…';
                    this._append(`<span class="ev-thinking">[thinking: ${esc(preview)}]</span>`);
                }
            } else if (bt === 'tool_use') {
                const id = block.id || '';
                const name = block.name || '';
                const input = block.input || {};
                const summary = formatToolInput(name, input);
                this._toolCalls.set(id, { name, summary });
                // Defer rendering until matching tool_result; render the
                // arrow line now so the user sees the call kick off, then
                // collapse on result.
                this._append(`<span class="ev-tool-call" data-tool-id="${esc(id)}">[→ ${esc(name)}${summary ? ' ' + esc(summary) : ''}]</span>`);
            } else if (bt === 'tool_result') {
                const tid = block.tool_use_id || '';
                const isErr = !!block.is_error;
                const preview = formatToolResult(block.content);
                const pending = this._toolCalls.get(tid);
                if (pending) {
                    this._toolCalls.delete(tid);
                    // Replace the tool-call line with the collapsed [Tool · args · preview].
                    const el = this._stream()?.querySelector(`[data-tool-id="${cssEsc(tid)}"]`);
                    if (el) {
                        const parts = [pending.name];
                        if (pending.summary) parts.push(pending.summary);
                        if (preview) parts.push(preview);
                        const cls = isErr ? 'ev-error' : 'ev-tool-result';
                        el.outerHTML = `<span class="${cls}">[${parts.map(esc).join(' · ')}]</span>`;
                        return;
                    }
                }
                const cls = isErr ? 'ev-error' : 'ev-tool-result';
                const label = isErr ? 'error' : 'result';
                this._append(`<span class="${cls}">[← ${label}: ${esc(preview)}]</span>`);
            }
        }
    }
}

function esc(s) {
    return String(s ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
}

function cssEsc(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function formatToolInput(name, inp) {
    if (!inp || typeof inp !== 'object') return '';
    if (name === 'Read' || name === 'Write' || name === 'Edit') return inp.file_path || '';
    if (name === 'Bash') {
        const cmd = inp.command || '';
        return cmd.length <= 80 ? cmd : cmd.slice(0, 77) + '…';
    }
    if (name === 'Grep' || name === 'Glob') return inp.pattern || '';
    if (name === 'WebFetch') return inp.url || '';
    if (name === 'WebSearch') return inp.query || '';
    const r = JSON.stringify(inp);
    return r.length <= 80 ? r : r.slice(0, 77) + '…';
}

function formatToolResult(content) {
    if (content == null) return '(no content)';
    let text = '';
    if (typeof content === 'string') text = content;
    else if (Array.isArray(content)) {
        for (const b of content) {
            if (b && b.type === 'text' && b.text) { text = b.text; break; }
        }
        if (!text) text = JSON.stringify(content);
    } else {
        text = JSON.stringify(content);
    }
    text = text.replace(/\n/g, ' ');
    return text.length <= 120 ? text : text.slice(0, 117) + '…';
}
