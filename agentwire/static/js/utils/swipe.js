/**
 * Shared horizontal-swipe detector — used by the desktop window cycler and the
 * mobile session cycler so the two surfaces can't drift apart.
 *
 * Single-finger by design: on a touch device a TWO-finger gesture is claimed by
 * the browser as pinch-zoom / a system gesture before JS sees clean move events,
 * so two-finger swipe-cycling can't be made reliable (no gesture library fixes
 * that — it's the OS/browser, not us). Pair this with `touch-action: pan-y` on
 * the surface so the browser keeps vertical scroll native and hands horizontal
 * drags to us instead of treating them as scroll / back-forward navigation.
 *
 * onSwipe(direction): direction is +1 for a left swipe (→ next), -1 for right
 * (→ previous), matching Tab / Shift+Tab.
 *
 * opts.threshold  px of horizontal travel required (default 50)
 * opts.ignore(target)  return true to skip a gesture starting on that element
 * opts.capture  listen in the capture phase (default false) — use it when a
 *               child (e.g. an xterm terminal) might swallow bubbling touches
 */
export function attachHorizontalSwipe(surface, onSwipe, opts = {}) {
    const threshold = opts.threshold ?? 50;
    const ignore = opts.ignore || (() => false);
    const capture = !!opts.capture;

    let active = false;
    let startX = 0, startY = 0, curX = 0, curY = 0;
    let lockedVertical = false;

    surface.addEventListener('touchstart', (e) => {
        // Single finger only; let multi-touch (zoom) and opted-out targets pass.
        if (e.touches.length !== 1 || (e.target && ignore(e.target))) {
            active = false;
            return;
        }
        active = true;
        lockedVertical = false;
        const p = e.touches[0];
        startX = curX = p.clientX;
        startY = curY = p.clientY;
    }, { passive: true, capture });

    surface.addEventListener('touchmove', (e) => {
        if (!active) return;
        const p = e.touches[0];
        curX = p.clientX;
        curY = p.clientY;
        const dx = curX - startX;
        const dy = curY - startY;
        // First decisive axis wins: vertical → release for native scroll;
        // horizontal → claim the gesture (block scroll / back-nav under it).
        if (!lockedVertical && Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 12) {
            lockedVertical = true;
        }
        if (!lockedVertical && Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 8) {
            e.preventDefault();
        }
    }, { passive: false, capture });

    surface.addEventListener('touchend', () => {
        if (!active) return;
        active = false;
        if (lockedVertical) return;
        const dx = curX - startX;
        const dy = curY - startY;
        if (Math.abs(dx) < threshold || Math.abs(dx) <= Math.abs(dy)) return;
        onSwipe(dx < 0 ? 1 : -1);  // swipe left → next, right → previous
    }, { passive: true, capture });
}
