/**
 * Session classification — which sessions are infrastructure "services"
 * (portal, scheduler, TTS/STT, config-defined custom services) vs working
 * sessions. Single source of truth shared by the desktop sidebar, the
 * command palette, and the mobile page.
 */

import { apiFetch } from './api.js';

const SERVICE_SESSIONS = new Set([
    'agentwire-portal',
    'agentwire-tts',
    'agentwire-stt',
    'agentwire-scheduler',
    'agentwire-notifications',
]);

export function isService(name) { return SERVICE_SESSIONS.has(name); }

// Merge config-defined custom services (services.custom in config.yaml) into
// the built-in allowlist. Idempotent — concurrent callers share one fetch.
// Resolves true if any names were added, so callers can re-render.
let loadPromise = null;
export function loadCustomServices() {
    if (!loadPromise) {
        loadPromise = (async () => {
            try {
                const res = await apiFetch('/api/services/custom');
                if (!res.ok) return false;
                const { names } = await res.json();
                let changed = false;
                for (const n of names || []) {
                    if (!SERVICE_SESSIONS.has(n)) { SERVICE_SESSIONS.add(n); changed = true; }
                }
                return changed;
            } catch {
                // Portal offline / endpoint missing — built-in services still group fine.
                return false;
            }
        })();
    }
    return loadPromise;
}
