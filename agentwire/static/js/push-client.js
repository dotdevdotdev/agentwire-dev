/**
 * Web Push client (#483).
 *
 * Registers the root-scoped service worker, then — if the owner has enabled push
 * and notification permission is granted — subscribes this browser to Web Push
 * and hands the subscription to the portal (`/api/push/subscribe`). After that,
 * the portal can buzz this device even when the tab is backgrounded or the phone
 * is locked, provided the PWA was installed (add-to-home-screen) on iOS.
 *
 * This module is intentionally quiet: it never prompts for permission on its own
 * (that must be a user gesture — wire `enablePush()` to a button), and it no-ops
 * cleanly when push is unsupported or the server reports push disabled.
 */

import { apiFetch } from './api.js';

const SW_URL = '/service-worker.js';

function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
    return output;
}

function pushSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/** Register the service worker (safe to call on every load). Returns the registration or null. */
export async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return null;
    try {
        return await navigator.serviceWorker.register(SW_URL, { scope: '/' });
    } catch (e) {
        console.warn('[push] SW registration failed:', e);
        return null;
    }
}

/** Fetch the portal's push config: { enabled, vapidPublicKey }. */
async function fetchPushConfig() {
    try {
        const res = await apiFetch('/api/push/config');
        if (!res.ok) return { enabled: false };
        return await res.json();
    } catch (e) {
        return { enabled: false };
    }
}

/**
 * Subscribe this browser to Web Push and POST the subscription to the portal.
 * Must be called from a user gesture the first time (it may prompt for
 * Notification permission). Returns true on success.
 */
export async function enablePush() {
    if (!pushSupported()) {
        console.warn('[push] Web Push not supported in this browser');
        return false;
    }

    const config = await fetchPushConfig();
    if (!config.enabled || !config.vapidPublicKey) {
        console.info('[push] Server push disabled or unconfigured — skipping subscribe');
        return false;
    }

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
        console.info('[push] Notification permission not granted');
        return false;
    }

    const registration = (await navigator.serviceWorker.ready) || (await registerServiceWorker());
    if (!registration) return false;

    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
        subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(config.vapidPublicKey),
        });
    }

    const json = subscription.toJSON();
    const res = await apiFetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            endpoint: json.endpoint,
            keys: json.keys,
            device: navigator.userAgent.slice(0, 120),
        }),
    });
    if (!res.ok) {
        console.warn('[push] subscribe POST failed:', res.status);
        return false;
    }
    console.info('[push] subscribed for Web Push');
    return true;
}

/** Unsubscribe this browser and tell the portal to drop the subscription. */
export async function disablePush() {
    if (!('serviceWorker' in navigator)) return;
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = registration && (await registration.pushManager.getSubscription());
    if (!subscription) return;
    const endpoint = subscription.endpoint;
    try {
        await subscription.unsubscribe();
    } catch (e) {
        /* best effort */
    }
    try {
        await apiFetch('/api/push/unsubscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint }),
        });
    } catch (e) {
        /* best effort */
    }
}

// Register the SW on load. If we already hold an active push subscription (the
// user opted in on a prior visit), silently refresh it with the server so a
// reinstalled portal re-learns this device. We never auto-prompt for permission.
if (pushSupported()) {
    registerServiceWorker().then(async () => {
        try {
            const registration = await navigator.serviceWorker.ready;
            const existing = await registration.pushManager.getSubscription();
            if (existing && Notification.permission === 'granted') {
                await enablePush();
            }
        } catch (e) {
            /* ignore */
        }
    });
    // Expose for a settings/Config toggle to wire a button to.
    window.agentwirePush = { enablePush, disablePush };
}
