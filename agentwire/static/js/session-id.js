/**
 * Session identity helpers — the canonical place for turning machine + session
 * fields into a session-id and back.
 *
 * Why this exists: different APIs disagree on how to encode "local". The
 * `/api/sessions/local` endpoint returns `machine: null`; the `/api/projects`
 * endpoint returns `machine: "local"`. The WS handler treats `<name>@local`
 * as a remote machine id and rejects it. Centralizing the conversion here
 * prevents that mismatch from biting consumers again.
 *
 * Encoding rules:
 *   - local machine: `machine` is null, undefined, or the string "local"
 *   - remote machine: `machine` is a non-empty string other than "local"
 *   - canonical session id:
 *       local  → `${name}`           (no @-suffix, ever)
 *       remote → `${name}@${machine}`
 *   - branch / worktree: `name` itself can contain a slash (`project/branch`);
 *     the parser keeps that intact.
 */

const LOCAL_SENTINEL = 'local';

/** Normalize a machine value to its canonical form (null = local). */
export function normalizeMachine(machine) {
    if (!machine || machine === LOCAL_SENTINEL) return null;
    return String(machine);
}

/** True when the two machine values refer to the same machine. */
export function sameMachine(a, b) {
    return normalizeMachine(a) === normalizeMachine(b);
}

/** True when the given machine value refers to the local machine. */
export function isLocalMachine(machine) {
    return normalizeMachine(machine) === null;
}

/**
 * Build the canonical session id used everywhere (WS URLs, window-tracking
 * Maps, taskbar buttons). Local machines never get a suffix.
 */
export function buildSessionId(name, machine) {
    const m = normalizeMachine(machine);
    if (!name) return '';
    if (m === null) return name;
    // Defensive: callers sometimes pass a name that already carries the suffix
    // (e.g. when echoing a session record they just rendered). Don't double it.
    if (name.endsWith(`@${m}`)) return name;
    return `${name}@${m}`;
}

/**
 * Parse a canonical session id into its parts. Inverse of buildSessionId,
 * tolerant of inputs that lack an @-suffix or that explicitly carry @local.
 *
 * @returns {{ name: string, machine: string|null }}
 */
export function parseSessionId(id) {
    if (!id) return { name: '', machine: null };
    const at = id.lastIndexOf('@');
    if (at === -1) return { name: id, machine: null };
    const machine = normalizeMachine(id.slice(at + 1));
    return { name: id.slice(0, at), machine };
}

/** True when two (name, machine) records refer to the same session. */
export function sameSession(a, b) {
    return (a?.name || null) === (b?.name || null) && sameMachine(a?.machine, b?.machine);
}
