import {
    getTerminalFontSize, getOverride,
    setTerminalFontSize, clearTerminalFontSize,
    FONT_SIZE_MIN, FONT_SIZE_MAX, FONT_SIZE_EVENT,
} from '../terminal-font-prefs.js';

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
            const res = await fetch('/api/config?format=display');
            const data = await res.json();
            const items = data.items || [];
            const itemHtml = items.map(({ key, value }) => {
                let display = value;
                if (value === null || value === undefined) display = '<em>null</em>';
                else if (typeof value === 'boolean') display = value ? '✓' : '✗';
                else if (typeof value === 'object') display = `<code>${JSON.stringify(value)}</code>`;
                return `<div class="sidebar-config-item"><span class="sidebar-config-key">${key}</span><span class="sidebar-config-val">${display}</span></div>`;
            }).join('');
            body.innerHTML = renderDisplayPrefs() + itemHtml;
            bindDisplayPrefs(body);
        } catch (e) {
            body.innerHTML = renderDisplayPrefs() + '<div class="sidebar-empty">Failed to load config</div>';
            bindDisplayPrefs(body);
        }
    },
};
