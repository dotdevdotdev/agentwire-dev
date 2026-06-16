import { apiFetch } from '../api.js';
import {
    getTerminalFontSize, getOverride,
    setTerminalFontSize, clearTerminalFontSize,
    FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_EVENT,
} from '../terminal-font-prefs.js';
import {
    getPermission, isMuted, setMuted, enableNotifications,
} from '../notification-prefs.js';
import { isAutoSend, setAutoSend, AUTOSEND_EVENT } from '../voice/autosend-prefs.js';

function renderDisplayPrefs() {
    const current = getTerminalFontSize();
    const isOverride = getOverride() !== null;
    return `<div class="sidebar-display-prefs">
        <div class="sidebar-display-row">
            <label class="sidebar-config-key" for="termFontSize">Terminal font</label>
            <span class="sidebar-display-value" data-display="size">${current}px${isOverride ? '' : ' <em>(auto)</em>'}</span>
        </div>
        <div class="sidebar-display-row">
            <input type="range" id="termFontSize" min="${FONT_SIZE_MIN}" max="${FONT_SIZE_MAX}" step="1" value="${current}" />
            <button class="sidebar-display-reset" data-action="reset" title="Reset to auto">↺</button>
        </div>
    </div>`;
}

function renderNotificationPrefs() {
    const perm = getPermission();
    const muted = isMuted();

    let status, control = '';
    if (perm === 'unsupported') {
        status = '<em>not supported</em>';
    } else if (perm === 'denied') {
        status = '<em>blocked</em>';
        control = '<span class="sidebar-display-hint">Allow in browser site settings</span>';
    } else if (perm === 'default') {
        status = '<em>off</em>';
        control = '<button class="sidebar-display-reset" data-action="enable-notifs">Enable</button>';
    } else { // granted
        status = muted ? 'muted' : 'on';
        control = `<label class="sidebar-notif-mute"><input type="checkbox" data-action="mute-notifs"${muted ? ' checked' : ''}/> Mute</label>`;
    }

    return `<div class="sidebar-display-prefs" data-notif-block>
        <div class="sidebar-display-row">
            <label class="sidebar-config-key">Desktop notifications</label>
            <span class="sidebar-display-value" data-notif="status">${status}</span>
        </div>
        ${control ? `<div class="sidebar-display-row">${control}</div>` : ''}
    </div>`;
}

function bindNotificationPrefs(body) {
    const repaint = () => {
        const block = body.querySelector('[data-notif-block]');
        if (!block) return;
        const tmp = document.createElement('div');
        tmp.innerHTML = renderNotificationPrefs();
        block.replaceWith(tmp.firstElementChild);
        wire();
    };
    function wire() {
        body.querySelector('[data-action="enable-notifs"]')?.addEventListener('click', async () => {
            await enableNotifications();
            repaint();
        });
        body.querySelector('[data-action="mute-notifs"]')?.addEventListener('change', (e) => {
            setMuted(e.target.checked);
            repaint();
        });
    }
    wire();
}

function renderAutoSendPref() {
    const on = isAutoSend();
    return `<div class="sidebar-display-prefs" data-autosend-block>
        <div class="sidebar-display-row">
            <label class="sidebar-config-key">Voice auto-send</label>
            <label class="sidebar-notif-mute"><input type="checkbox" data-action="voice-autosend"${on ? ' checked' : ''}/> ${on ? 'on' : 'off'}</label>
        </div>
        <div class="sidebar-display-row">
            <span class="sidebar-display-hint">Skip transcript review — send on release</span>
        </div>
    </div>`;
}

function bindAutoSendPref(body) {
    const repaint = () => {
        const block = body.querySelector('[data-autosend-block]');
        if (!block) return;
        const tmp = document.createElement('div');
        tmp.innerHTML = renderAutoSendPref();
        block.replaceWith(tmp.firstElementChild);
        wire();
    };
    function wire() {
        body.querySelector('[data-action="voice-autosend"]')?.addEventListener('change', (e) => {
            setAutoSend(e.target.checked);
        });
    }
    wire();
    window.addEventListener(AUTOSEND_EVENT, repaint);
    body._autoSendRepaint = repaint;
}

function bindDisplayPrefs(body) {
    const slider = body.querySelector('#termFontSize');
    const valueEl = body.querySelector('[data-display="size"]');
    const resetBtn = body.querySelector('[data-action="reset"]');
    if (!slider) return;

    const repaint = () => {
        const current = getTerminalFontSize();
        const isOverride = getOverride() !== null;
        slider.value = current;
        if (valueEl) valueEl.innerHTML = `${current}px${isOverride ? '' : ' <em>(auto)</em>'}`;
    };

    slider.addEventListener('input', () => setTerminalFontSize(slider.value));
    resetBtn?.addEventListener('click', () => clearTerminalFontSize());
    window.addEventListener(FONT_SIZE_EVENT, repaint);
    body._fontPrefRepaint = repaint;
}

export const configSection = {
    title: 'Config',
    async mount(body) { await this.refresh(body); },
    async refresh(body) {
        try {
            const res = await apiFetch('/api/config?format=display');
            const data = await res.json();
            const items = data.items || [];
            const itemHtml = items.map(({ key, value }) => {
                let display = value;
                if (value === null || value === undefined) display = '<em>null</em>';
                else if (typeof value === 'boolean') display = value ? '✓' : '✗';
                else if (typeof value === 'object') display = `<code>${JSON.stringify(value)}</code>`;
                return `<div class="sidebar-config-item"><span class="sidebar-config-key">${key}</span><span class="sidebar-config-val">${display}</span></div>`;
            }).join('');
            body.innerHTML = renderDisplayPrefs() + renderNotificationPrefs() + renderAutoSendPref() + itemHtml;
            bindDisplayPrefs(body);
            bindNotificationPrefs(body);
            bindAutoSendPref(body);
        } catch (e) {
            body.innerHTML = renderDisplayPrefs() + renderNotificationPrefs() + renderAutoSendPref() + '<div class="sidebar-empty">Failed to load config</div>';
            bindDisplayPrefs(body);
            bindNotificationPrefs(body);
            bindAutoSendPref(body);
        }
    },
};
