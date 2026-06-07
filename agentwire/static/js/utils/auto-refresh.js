/**
 * Auto-refresh utility for progressive loading with "checking" states.
 *
 * Pattern:
 * 1. Initial fetch returns items with status="checking" (spinners show)
 * 2. Items are rendered immediately
 * 3. Auto-refresh triggers after delay
 * 4. Subsequent fetch uses cached status (spinners → real status)
 * 5. Continues until no items are "checking"
 *
 * Usage:
 *     async function fetchMachines() {
 *         const machines = await apiFetch('/api/machines').then(r => r.json());
 *         setupAutoRefresh(machines, machinesWindow, 'status', 'checking', 2000);
 *         return machines;
 *     }
 */

import { apiFetch } from '../api.js';

/**
 * Setup auto-refresh for items with checking status
 *
 * @param {Array} items - Array of items to check
 * @param {Object} window - ListWindow instance with refresh() method
 * @param {string} statusField - Field name to check (default: 'status')
 * @param {string} checkingValue - Value indicating still checking (default: 'checking')
 * @param {number} delayMs - Delay before refresh in ms (default: 2000)
 */
export function setupAutoRefresh(
    items,
    window,
    statusField = 'status',
    checkingValue = 'checking',
    delayMs = 2000
) {
    const hasChecking = items.some(item => item[statusField] === checkingValue);

    if (hasChecking && window?.refresh) {
        setTimeout(() => {
            window.refresh();
        }, delayMs);
    }
}

/**
 * Determine status for rendering (handles checking state)
 *
 * @param {Object} item - Item to render
 * @param {string} statusField - Field name to check (default: 'status')
 * @param {string} checkingValue - Value indicating still checking (default: 'checking')
 * @param {string} onlineValue - Value indicating online (default: 'online')
 * @returns {Object} - { isChecking: boolean, isOnline: boolean }
 */
export function getStatusForRendering(
    item,
    statusField = 'status',
    checkingValue = 'checking',
    onlineValue = 'online'
) {
    const status = item[statusField];
    return {
        isChecking: status === checkingValue,
        isOnline: status === onlineValue
    };
}
