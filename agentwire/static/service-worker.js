/* AgentWire service worker (#483).
 *
 * Served from the site root (`/service-worker.js`) so its scope is `/` — that is
 * what lets one SW receive push for the whole portal. It deliberately does NOT
 * cache app shell / API responses: the portal is a live control surface where a
 * stale cached view would be actively misleading. Its sole job is Web Push so
 * notifications reach a backgrounded/locked phone without a tab being awake.
 */

const ICON = '/static/img/icon-192.png';

// Take control immediately on install/activate so push works on first load.
self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (e) {
        payload = { title: 'AgentWire', body: event.data ? event.data.text() : '' };
    }

    const title = payload.title || 'AgentWire';
    const options = {
        body: payload.body || '',
        icon: ICON,
        badge: ICON,
        tag: payload.tag || 'agentwire',
        renotify: true,
        data: { url: payload.url || '/' },
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const target = (event.notification.data && event.notification.data.url) || '/';

    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
            // Focus an existing portal tab if one is open; otherwise open a new one.
            for (const client of clients) {
                if ('focus' in client) {
                    client.navigate(target).catch(() => {});
                    return client.focus();
                }
            }
            if (self.clients.openWindow) {
                return self.clients.openWindow(target);
            }
        })
    );
});
