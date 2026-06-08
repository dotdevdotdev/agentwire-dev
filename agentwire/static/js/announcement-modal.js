/**
 * Announcement modal — once-per-announcement welcome card for the userbase.
 *
 * Content is a single JSON file (agentwire/static/announcements.json) edited
 * on `main`. It plays two roles:
 *   - Remote source: fetched from raw.githubusercontent on load, so an edit
 *     on `main` reaches every user on their next portal open — no upgrade.
 *   - Bundled fallback: the same file ships in the package and is served at
 *     /static/announcements.json, used when the remote GET fails (offline).
 *
 * The fetch is a single unauthenticated GET with nothing about the user in
 * it — no telemetry. Content fields are rendered as ESCAPED TEXT (the
 * frontend owns all markup), so even though the body comes from a URL there
 * is no injection surface; the CTA url is validated to http(s).
 *
 * Show logic: the newest announcement whose id isn't in the dismissed set
 * (localStorage). Dismiss via the button, the ✕, Esc, or backdrop click.
 */

const REMOTE_URL = 'https://raw.githubusercontent.com/dotdevdotdev/agentwire-dev/main/agentwire/static/announcements.json';
const LOCAL_URL = '/static/announcements.json';
const SEEN_KEY = 'aw-announcements-seen';
const FETCH_TIMEOUT_MS = 4000;

let modalEl = null;
let escHandler = null;

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function seenIds() {
    try {
        const raw = localStorage.getItem(SEEN_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr : [];
    } catch {
        return [];
    }
}

function markSeen(id) {
    const ids = seenIds();
    if (!ids.includes(id)) {
        ids.push(id);
        try { localStorage.setItem(SEEN_KEY, JSON.stringify(ids)); } catch { /* ignore */ }
    }
}

async function fetchJson(url) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
    try {
        // No credentials, no caching, nothing about the user goes out.
        const resp = await fetch(url, { signal: ctrl.signal, cache: 'no-store', credentials: 'omit' });
        if (!resp.ok) return null;
        return await resp.json();
    } catch {
        return null;
    } finally {
        clearTimeout(timer);
    }
}

/** A valid, safe http(s) URL, or null. */
function safeUrl(url) {
    try {
        const u = new URL(String(url));
        return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null;
    } catch {
        return null;
    }
}

/** First announcement (in file order — newest first) the user hasn't dismissed. */
function pickUnseen(data) {
    const list = Array.isArray(data?.announcements) ? data.announcements : [];
    const seen = seenIds();
    return list.find((a) => a && typeof a.id === 'string' && a.id && !seen.includes(a.id)) || null;
}

function renderModal(a) {
    const emoji = a.emoji ? `<div class="announcement-emoji">${escapeHtml(a.emoji)}</div>` : '';
    const date = a.date ? `<div class="announcement-date">${escapeHtml(a.date)}</div>` : '';
    const highlights = Array.isArray(a.highlights) && a.highlights.length
        ? `<ul class="announcement-highlights">${a.highlights
            .map((h) => `<li>${escapeHtml(h)}</li>`).join('')}</ul>`
        : '';
    const ctaUrl = a.cta && safeUrl(a.cta.url);
    const cta = ctaUrl
        ? `<a class="announcement-cta" href="${escapeHtml(ctaUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(a.cta.label || 'Learn more')}</a>`
        : '';
    return `<div class="modal-overlay announcement-overlay" id="announcementOverlay">
        <div class="modal announcement-modal" role="dialog" aria-label="Announcement" aria-modal="true">
            <button class="announcement-dismiss" data-action="dismiss" title="Dismiss" aria-label="Dismiss">✕</button>
            ${emoji}
            <h2 class="announcement-title">${escapeHtml(a.title || 'What’s new')}</h2>
            ${date}
            <div class="announcement-body">${escapeHtml(a.body || '')}</div>
            ${highlights}
            <div class="announcement-footer">
                ${cta}
                <button class="announcement-ok" data-action="dismiss">Got it</button>
            </div>
        </div>
    </div>`;
}

function close(id) {
    if (id) markSeen(id);
    if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
    modalEl?.remove();
    modalEl = null;
}

function show(a) {
    if (modalEl) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderModal(a);
    modalEl = wrapper.firstElementChild;
    document.body.appendChild(modalEl);

    modalEl.addEventListener('click', (e) => {
        if (e.target === modalEl || e.target.closest('[data-action="dismiss"]')) {
            close(a.id);
        }
    });
    escHandler = (e) => { if (e.key === 'Escape') close(a.id); };
    document.addEventListener('keydown', escHandler);
}

/**
 * Fetch announcements (remote first, bundled fallback) and show the newest
 * unseen one. Fails silently — an announcement is never worth a broken portal.
 */
export async function initAnnouncements() {
    try {
        const data = (await fetchJson(REMOTE_URL)) || (await fetchJson(LOCAL_URL));
        if (!data) return;
        const a = pickUnseen(data);
        if (a) show(a);
    } catch {
        /* never let an announcement break the desktop */
    }
}
