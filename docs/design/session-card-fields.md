# Session Card — Fields We Can Show

Everything we can surface about a session (and its parent/children) on a HUD /
topology card. Input for card-design work (incl. the claude.ai/design research
pass). Each field tagged by availability:

- **[live]** — already on the `/api/sessions` feed (`list_local_sessions`) or the `activityStates` map
- **[live+ctx]** — on the feed only when it's fetched with `show_context` (model / context %)
- **[worktree]** — on `/api/worktrees` (`worktree --list --all`); folds in local git status
- **[derive]** — computable client-side from data we already have
- **[plumb]** — needs new backend/feed wiring

## Identity
| Field | Avail | Notes |
|-------|-------|-------|
| name | live | full tmux session name |
| display name | derive | branch/worktree tail for children (`cardDisplayName`, #792) |
| machine | live | host — `local` or a remote id (`⌂ host`) |
| project / repo | derive→plumb | the repo it belongs to (infer from path; explicit would need plumbing) |
| working dir (path) | live | `pane_current_path` |
| worktree path | worktree | `~/worktrees/<proj>/<branch>/` |
| branch | worktree | git branch |

## Topology & role — the three axes (#716)
| Field | Avail | Notes |
|-------|-------|-------|
| role | live | `orchestrator` \| `worker` (fallback: parent-ness) |
| topology | derive→plumb | `main` \| `worktree` \| `pane` |
| rooting | live | `root` \| `parented` (`created_by`) |
| parent | live | `created_by` / display parent |
| roles[] | live | persona/etiquette list from `.agentwire.yml` — `soul`, `correspondent`, `anchor`, … |
| posture | live | `bypass` \| `prompted` \| `auto` \| `bare` (#729) |
| children count | derive | from the tree |
| depth / sibling index | derive | row + nesting position |

## Live state / activity
| Field | Avail | Notes |
|-------|-------|-------|
| activity | live | `idle` \| `processing` \| `generating` \| `playing` (`activityStates`) |
| wire state | derive | `idle` \| `flow` \| `awaiting` \| `stuck` (`wireStateFor`) |
| computed state | live | `needs_input` \| `off` \| `working` \| `idle` |
| self / you-are-here | derive | is this the focused session |
| usage-limit parked | live | `usage_limit` flag |
| windows count | live | tmux windows |
| last-active / idle duration | plumb | not currently tracked per-session |

## Agent context (Claude Code)
| Field | Avail | Notes |
|-------|-------|-------|
| is_agent | live+ctx | running Claude Code |
| model | live+ctx | `claude-opus-4-8`, … |
| context remaining % | live+ctx | `remaining_pct` |
| context flagged / note | live+ctx | low-context / custom note |

## Git — worktree sessions (drives the phantom-card ask)
| Field | Avail | Notes |
|-------|-------|-------|
| dirty (staged/unstaged/untracked) | worktree | with counts |
| clean | worktree | working tree clean |
| ahead / behind upstream | worktree | `↑n` / `↓n` |
| pushed / unpushed | worktree | `unpushed` = no upstream |
| upstream set | worktree | — |

## PR / issue linkage
| Field | Avail | Notes |
|-------|-------|-------|
| open PR # + state | plumb→derive | draft/open/merged; dangling-PR detection already exists (#716) |
| linked issue | plumb | from PR body `Closes #N` |

## Comms / voice
| Field | Avail | Notes |
|-------|-------|-------|
| PTT / mic available | derive | voice enabled for the session |
| unread inbox count | plumb | `msg` channel — `~/.agentwire/inbox/<session>/` |
| pending routed prompt | plumb | a prompt awaiting an answer (#276) |

## Ghost / orphan — phantom cards (session-less worktree)
| Field | Avail | Notes |
|-------|-------|-------|
| exists on disk | worktree | — |
| alive (has session) | worktree | phantom = exists && !alive |
| orphan state | derive | `state: 'orphan'` |
| created_by lineage | worktree | for grouping under its repo/family |

## Visual / derived
| Field | Avail | Notes |
|-------|-------|-------|
| lineage tint (family color) | derive | hue per family |
| status dot color | derive | from wire state |
| activity sparkline / history | derive→plumb | current: instantaneous bars; real history needs plumbing |

---

### Design goals (for any card redesign)
- **Stay compact and focused** — the HUD's whole value is a dense, glanceable map; do not turn cards into dashboards.
- **Spend width, not clutter** — cards were widened (176px, self 208px); use the room for the *distinguishing* info, not everything above.
- **Brand:** neon green `#00ff66` + neon blue `#00bfff` on near-black; family cards tinted by lineage hue; frosted-glass shade.
- **Narrow viewport** — the portal runs ~1/3 screen width (~600px); the shade scrolls horizontally. Design for narrow, not a wide canvas.
- **State legible at a glance** — a card should read its status (working / idle / awaiting / stuck) and lineage without reading text.
