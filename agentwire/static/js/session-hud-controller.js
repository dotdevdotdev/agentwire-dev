/**
 * session-hud-controller.js
 *
 * The context-following brain behind the Session HUD shade (#778): decides
 * WHAT the drawer renders, and wires up card-click → mini-terminal the same
 * way the Session Workspace window does.
 *
 * Mounts a `TopologyView(mode:'shade')` (#777) into the HUD's canvas and
 * subscribes to two feeds:
 *   - the sessions feed (`onSessionsChanged`, sidebar/sessions-section.js)
 *   - window-focus changes (`desktop`'s `active_window_changed`, fired by
 *     every window kind's onFocus → `desktop.setActiveWindow`)
 *
 * Context-following view selection:
 *   - No session window focused → render ALL root families (the global
 *     tree) — just `render(sessions)`, letting TopologyView's own
 *     `groupFamilies` call do the grouping.
 *   - A session window focused → re-root onto that session's family
 *     (`subtreeOf`, lineage.js) and present it as if its card were clicked:
 *     the focused session becomes a dimmed, non-interactive "you-are-here"
 *     root (`TopologyView.setSelfSession`); its descendants are the
 *     interactive cards.
 * Focusing an artifact/review/workspace/council window resolves to no
 * session (`resolveWindowSession` returns null for non-`kind:'session'`
 * windows) and is therefore a no-op — the last session context is retained,
 * per spec. `resolveWindowSession` is injected via `init()` rather than
 * imported from desktop.js directly: desktop.js is the module that boots
 * this controller, and desktop.js↔session-hud-controller.js would otherwise
 * be a static circular import (the same reason `card-terminal.js`'s
 * "open full terminal" button dynamic-imports desktop.js instead).
 *
 * Ghost cards (#781): worktree folders left on disk with no live session
 * (`agentwire worktree --list`'s "orphan" state) are polled from
 * `/api/worktrees` separately from the live sessions feed — they're not
 * sessions, so they don't belong in sidebar/sessions-section.js's shared
 * `getAllSessions()` — and merged into the array passed to `render()` as
 * pseudo-session records (`state: 'orphan'`, plus `branch`/`worktreePath`/
 * `projectPath`/`git` — the same read-only git-status shape the sidebar
 * badges worktree sessions with, #801). Each ghost's `parent` is whatever
 * `--list` resolved as its dead session's recorded creator (may be
 * undefined): `groupFamilies`/`lineageOf` (lineage.js) already treat an
 * unresolvable parent name as "this is its own root", so in the global tree
 * a ghost with no known lineage naturally lands as its own single-card
 * family with zero extra grouping logic here. Once a session is focused,
 * `_scopedGhosts` additionally scopes the fetched list to "the repo you're
 * looking at" (#801) — see its own doc comment for the two cases it
 * distinguishes (lineage-linked ghosts vs. unattached ones grafted onto the
 * focused session when their repo matches).
 */

import { desktop } from './desktop-manager.js';
import { TopologyView } from './topology-render.js';
import { getAllSessions, onSessionsChanged, ensureSessionsLoaded } from './sidebar/sessions-section.js';
import { subtreeOf } from './lineage.js';
import { mountCardTerminal, mountSelfMic } from './card-terminal.js';
import { sessionHud } from './session-hud.js';
import { apiFetch } from './api.js';
import { normalizeMachine } from './session-id.js';
import { toastSuccess, toastError } from './toast.js';
import { projectFromCwd } from './safety-shared.js';

const GHOST_POLL_MS = 20000;

/** Fallback-tier repo name from a filesystem path, for when a session has no
 * entry in `_sessionProject` (the authoritative, server-resolved lookup
 * `_scopedGhosts` prefers — see there). No explicit "project" field is
 * plumbed onto live session records (docs/design/session-card-fields.md tags
 * it "derive→plumb"), so this is a best-effort guess from the path's shape:
 * reuses `projectFromCwd` (safety-shared.js, already handles a plain
 * `~/projects/<project>/` checkout and the legacy scheduler-dispatch
 * `~/projects/<project>-worktrees/<branch>/` layout), layering in the
 * `agentwire worktree` default layout (`~/worktrees/<project>/<branch>/`,
 * CLAUDE.md) it doesn't cover. Both this and `projectFromCwd`'s "projects"
 * lookup are name-based and go stale under a non-default `worktree.dir`
 * override — a gap `_sessionProject` (an exact, config-independent lookup)
 * doesn't have, which is why that's the primary path and this is only the
 * fallback for sessions never registered via `agentwire worktree`.
 * @param {string|null|undefined} path
 * @returns {string|null}
 */
function repoNameFromPath(path) {
    if (!path) return null;
    const parts = String(path).replace(/\/+$/, '').split('/').filter(Boolean);
    if (!parts.length) return null;
    const wtIdx = parts.lastIndexOf('worktrees');
    if (wtIdx !== -1 && wtIdx + 1 < parts.length) return parts[wtIdx + 1];
    const viaProjects = projectFromCwd(path);
    return viaProjects && viaProjects !== '—' ? viaProjects : parts[parts.length - 1];
}

class HudController {
    constructor() {
        this._view = null;
        this._resolveWindowSession = null;
        /** @type {string|null} focused session name, or null = global tree */
        this._contextSession = null;
        /** @type {boolean} "master" mode (showAll) — pin the global tree and
         * ignore focus re-rooting until the drawer is closed. */
        this._global = false;
        /** @type {string|null} window id backing _contextSession, for window_unregistered matching */
        this._contextWindowId = null;
        /** @type {Array<object>} pseudo-session records for orphaned worktrees, refreshed via polling */
        this._ghosts = [];
        /** @type {Map<string, string>} session name → repo root path, from EVERY
         * worktree-registry entry (alive and dead) the last `/api/worktrees`
         * poll returned — exact and server-resolved (git_root()), so repo
         * scoping (#801) prefers this over guessing from a live pane cwd. */
        this._sessionProject = new Map();
        this._ghostTimer = null;
    }

    /**
     * @param {HTMLElement} canvas - `.session-hud-canvas`, sessionHud's mount point
     * @param {(id: string|null) => string|null} resolveWindowSession - desktop.js's
     *   getWindowSession — resolves a focused window id to a session name, or null
     *   for a non-session window (or nothing focused)
     */
    init(canvas, resolveWindowSession) {
        this._resolveWindowSession = resolveWindowSession;

        this._view = new TopologyView(canvas, {
            mode: 'shade',
            onCardExpand: (name, session, slotEl) => this._expandCard(name, session, slotEl),
            onSelfMount: (name, session, cardEl) => mountSelfMic(name, session, cardEl),
            onGhostCleanup: (name, session) => this._cleanupGhost(name, session),
            onGhostAdopt: (name, session) => this._adoptGhost(name, session),
            onCardOpen: (name, session) => this._openSession(name, session),
            onCardKill: (name, session) => this._killSession(name, session),
        });

        // Seed from whatever's already focused when the HUD first mounts,
        // rather than waiting for the next focus change.
        this._applyFocus(desktop.getActiveWindow());

        ensureSessionsLoaded();
        this._render();
        onSessionsChanged(() => this._render());

        this._fetchGhosts();
        this._ghostTimer = setInterval(() => this._fetchGhosts(), GHOST_POLL_MS);

        // Closing the drawer exits "master" mode, so the next Alt+P open is the
        // normal context-following view again.
        sessionHud.onClose(() => { this._global = false; });

        desktop.on('active_window_changed', ({ id }) => this._applyFocus(id));
        // Closing the focused session's own window (with nothing else taking
        // focus) falls back to the global tree, rather than leaving the HUD
        // re-rooted onto a session that's no longer open anywhere.
        desktop.on('window_unregistered', ({ id }) => {
            if (id && id === this._contextWindowId) {
                this._contextSession = null;
                this._contextWindowId = null;
                this._render();
            }
        });
    }

    _applyFocus(id) {
        if (this._global) return; // master mode pins the global tree
        const session = this._resolveWindowSession(id);
        if (!session || session === this._contextSession) return;
        this._contextSession = session;
        this._contextWindowId = id;
        this._render();
    }

    /**
     * "Master" view — open the HUD showing the full session tree (every family,
     * not the focused session's subtree) and pin it there: focus changes no
     * longer re-root until the drawer closes. Entry points are the Sessions
     * sidebar-header icon and the "Show all sessions" command-palette action
     * (no hotkey — the plain Alt+P peek is the context-following view). Idempotent.
     */
    showAll() {
        this._global = true;
        this._contextSession = null;
        this._contextWindowId = null;
        if (sessionHud.segment !== 'sessions') sessionHud.setSegment('sessions');
        if (!sessionHud.open) sessionHud.toggle(true);
        this._render();
    }

    _render() {
        if (!this._view) return;
        const sessions = getAllSessions();
        let contextRecord = this._contextSession ? sessions.find((s) => s.name === this._contextSession) : null;
        // The context session may have closed/renamed since it was last
        // focused — fall back to the global tree rather than rendering an
        // empty subtree for a name nothing matches anymore. Gated on
        // sessions.length: an empty list during the page-boot window (the
        // sessions fetch hasn't resolved yet, e.g. mid-restoreTaskbarState())
        // means "no data yet", not "this session is gone" — resetting on
        // that would wipe a just-restored focus before it ever got to render.
        if (this._contextSession && sessions.length > 0 && !contextRecord) {
            this._contextSession = null;
            this._contextWindowId = null;
            contextRecord = null;
        }
        const ghosts = this._scopedGhosts(sessions, contextRecord);
        const merged = ghosts.length ? [...sessions, ...ghosts] : sessions;
        this._view.setSelfSession(this._contextSession);
        this._view.render(this._contextSession ? subtreeOf(this._contextSession, merged) : merged);
    }

    /**
     * Phantom cards scoped to "the repo you're looking at" (#801). Two cases,
     * kept deliberately separate:
     *   - A ghost with a resolvable `.parent` is already reachable through
     *     normal family lineage (`groupFamilies`/`subtreeOf`, lineage.js) and
     *     always shows under that family, whatever repo it's actually in —
     *     cross-project parenting via `--created-by` is a supported pattern
     *     (CLAUDE.md's rooting section), so repo-scoping must never hide it.
     *   - A ghost with NO resolvable parent (its creator session is also
     *     gone, or was never recorded) is the "unattached, needs cleanup"
     *     case — today it surfaces only as its own standalone-root family in
     *     the global tree and is invisible from any focused view at all
     *     (`subtreeOf` only walks `.parent` reachability, so nothing without
     *     a parent link can ever appear as a descendant). Once a session is
     *     focused, if such a ghost's repo matches the focused session's repo,
     *     graft it onto that session (synthetic `parent`) so it actually
     *     surfaces grouped with the family you're looking at instead of
     *     staying invisible; a different repo's unattached ghost is left out
     *     of this particular view (it still shows in the global tree).
     * With no session focused (global tree) there's no single "current repo"
     * to scope to, so every fetched ghost shows, same as before.
     */
    _scopedGhosts(sessions, contextRecord) {
        if (!this._ghosts.length || !this._contextSession) return this._ghosts;
        const repo = this._currentRepoName(contextRecord);
        if (!repo) return this._ghosts;
        const knownNames = new Set(sessions.map((s) => s.name));
        for (const g of this._ghosts) knownNames.add(g.name);
        const out = [];
        for (const g of this._ghosts) {
            if (g.parent && knownNames.has(g.parent)) { out.push(g); continue; }
            if (!g.projectPath || repoNameFromPath(g.projectPath) === repo) {
                // `parent` here is display-only (places the card inside the
                // focused family's block). `syntheticParent` makes the graft
                // render AS a graft (#955): topology-render.js draws no
                // connector wire and shows a "same repo — not a recorded
                // child" note, and `_adoptGhost` refuses to record it as the
                // real creator, since this ghost never actually had one.
                out.push({ ...g, parent: this._contextSession, syntheticParent: true });
            }
        }
        return out;
    }

    /** The focused session's repo, or null when it can't be determined
     * confidently (fail open — `_scopedGhosts` shows everything unfiltered
     * rather than risk hiding a real phantom card on a bad guess). Prefers
     * the exact, server-resolved `_sessionProject` lookup (immune to pane-cwd
     * drift and `worktree.dir` overrides) over the `repoNameFromPath`
     * heuristic, which only runs for a session never registered via
     * `agentwire worktree` (main/pane topology). Remote sessions (`machine`
     * set) have no local-registry signal to scope against — `/api/worktrees`
     * is local-machine only — so they're left unscoped too. */
    _currentRepoName(contextRecord) {
        if (!contextRecord || contextRecord.machine) return null;
        const project = this._sessionProject.get(this._contextSession);
        if (project) return repoNameFromPath(project);
        return repoNameFromPath(contextRecord.path);
    }

    _expandCard(name, session, slotEl) {
        sessionHud.growToHalf();
        const cleanup = mountCardTerminal(name, session, slotEl, { topologyView: this._view });
        return () => {
            cleanup();
            sessionHud.restoreDetent();
        };
    }

    // ⋯ menu "Open window" — pop the session into its own full terminal window.
    // Dynamic-imports desktop.js for the same reason card-terminal.js does: it's
    // the module that boots this controller, so a static import would be circular.
    async _openSession(name, session) {
        try {
            const { openSessionTerminal } = await import('./desktop.js');
            openSessionTerminal(name, 'terminal', normalizeMachine(session?.machine));
        } catch (e) {
            toastError(`Couldn't open ${name}: ${e.message}`);
        }
    }

    // ⋯ menu "Kill session" — the two-step confirm already happened in-menu
    // (topology-render.js), so this fires straight through. DELETE /api/sessions
    // is the same thin `agentwire kill` wrapper the sidebar close button uses; the
    // resulting lifecycle push re-renders the tree without this card.
    async _killSession(name, session) {
        try {
            const res = await apiFetch(`/api/sessions/${encodeURIComponent(name)}`, { method: 'DELETE' });
            if (!res.ok) {
                const reason = await res.text().catch(() => '') || `HTTP ${res.status}`;
                toastError(`Kill failed for ${name}: ${reason}`);
                return;
            }
            toastSuccess(`Killed ${name}`);
        } catch (e) {
            toastError(`Kill failed for ${name}: ${e.message}`);
        }
    }

    // Ghosts (#781) aren't sessions — no WS push tells us when a worktree dir
    // disappears or a dead one reappears — so this is a plain poll, refreshed
    // eagerly right after an action instead of waiting out the interval.
    async _fetchGhosts() {
        try {
            const res = await apiFetch('/api/worktrees');
            const data = await res.json();
            const entries = data.entries || [];
            this._ghosts = entries
                .filter((e) => e.exists && !e.alive)
                .map((e) => this._toGhostRecord(e));
            // Every registered worktree session (alive or dead), not just the
            // phantom ones above — the repo-scoping lookup (#801, `_currentRepoName`)
            // needs the CURRENTLY FOCUSED session's repo too, which is usually
            // alive and therefore filtered out of `this._ghosts`.
            this._sessionProject = new Map(
                entries.filter((e) => e.session && e.project).map((e) => [e.session, e.project])
            );
        } catch (e) {
            this._ghosts = [];
            this._sessionProject = new Map();
        }
        this._render();
    }

    _toGhostRecord(e) {
        const branch = e.branch || (e.worktree_path || '').split('/').filter(Boolean).pop() || e.session;
        return {
            name: e.session,
            parent: e.created_by || undefined,
            state: 'orphan',
            branch,
            worktreePath: e.worktree_path,
            projectPath: e.project,
            git: e.git || null,
        };
    }

    async _cleanupGhost(name, session) {
        try {
            const res = await apiFetch('/api/worktree/cleanup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: session.branch, project: session.projectPath }),
            });
            const result = await res.json().catch(() => ({}));
            if (!res.ok || result.success === false) {
                const reason = result.error || `HTTP ${res.status}`;
                toastError(`Clean up failed for ${name}: ${reason}`);
                return { error: reason };
            }
            toastSuccess(`Removed worktree ${session.branch || name}`);
            const note = (result.branch && !result.branch_deleted && result.branch_note)
                ? `Removed — branch kept: ${result.branch_note}`
                : null;
            await this._fetchGhosts();
            return note ? { note } : {};
        } catch (e) {
            toastError(`Clean up failed for ${name}: ${e.message}`);
            return { error: e.message };
        }
    }

    async _adoptGhost(name, session) {
        try {
            const res = await apiFetch('/api/worktree/adopt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: session.branch,
                    project: session.projectPath,
                    // A synthetic `parent` (#801 repo-scoping graft, `_scopedGhosts`)
                    // is display-only — this ghost never actually had a
                    // creator, so adopting it must not record one.
                    createdBy: session.syntheticParent ? undefined : (session.parent || undefined),
                }),
            });
            const result = await res.json().catch(() => ({}));
            if (!res.ok || result.success === false) {
                const reason = result.error || `HTTP ${res.status}`;
                toastError(`Adopt failed for ${name}: ${reason}`);
                return { error: reason };
            }
            toastSuccess(`Adopted ${result.session || name}`);
            await this._fetchGhosts();
            return {};
        } catch (e) {
            toastError(`Adopt failed for ${name}: ${e.message}`);
            return { error: e.message };
        }
    }
}

export const hudController = new HudController();
