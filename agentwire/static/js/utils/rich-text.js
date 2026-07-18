/**
 * rich-text.js — the one safe markdown-subset renderer for toast text.
 *
 * Escape everything FIRST so no source HTML survives, then introduce only
 * our own known tags: [links](url) (http/https/mailto only), **bold**, and
 * line breaks. Pure string-in/string-out — no DOM — so the rendering is
 * regression-testable under plain node (tests/unit/test_rich_text_js.py).
 *
 * `links: false` renders link syntax as LITERAL text instead of an anchor.
 * Artifact-notice toasts use it (#821 review): their text embeds a
 * caller-supplied artifact title, and the whole toast body is already a
 * click-to-open button — an embedded anchor would both fight that click
 * handler and let a title like "[Click Here](https://evil.example)" spoof
 * a trusted-looking link inside a system toast.
 */

export function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export function renderRichText(str, { links = true } = {}) {
    let s = escapeHtml(str);
    if (links) {
        // Links before bold so [**label**](url) composes. The url came
        // through escapeHtml, so any quote in it is already &quot; and
        // can't close the href attribute.
        s = s.replace(
            /\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
            (_m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
        );
    }
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\n/g, '<br>');
    return s;
}
