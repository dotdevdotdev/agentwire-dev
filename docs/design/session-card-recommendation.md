# Session Card Redesign — Recommendation (#799)

## Update: core recommendations shipped

The full-width pulse-strip activity visualization (item 2 below) shipped in
#809, replacing the sparkline exactly as proposed (`.topology-pulse-*` in
`agentwire/static/js/topology-render.js`/`desktop.css`). The git dirty/clean/
ahead-behind glyphs (part of item 3) shipped in #805
(`sidebar-git-badge`/`git-dirty`/`git-ahead`/`git-behind` etc.). The
context-remaining hairline and the header mic/inbox badge cluster are still
open. The rest of this doc — including the "outside-AI pass didn't
complete" status below — is kept as the original design-research record.

## Status: outside-AI pass did not complete — read this section first

The plan was to brief `claude.ai/design` with the current-state screenshots + the
field catalogue and relay back its recommendation. That got partway there before
the Chrome extension connection became unstable (the exact "tab accumulation
crashes Chrome" risk flagged in the task brief — except here it wasn't
accumulation, the *same* tab and its replacements kept dying within seconds of
creation/reconnect, independent of anything this session did). Three fresh tabs
in a row died immediately after creation; that's an environment-level Chrome
instability, not a retryable tool-call bug, so per the "don't rabbit-hole"
guidance this session stopped rather than continuing to hammer it.

**What did happen:**
- A Claude Design project was created and is live at:
  `https://claude.ai/design/p/716f1cf9-7bab-4f78-9812-2ab07f983f85` ("AgentWire
  Session Cards"). It had a file `Session Topology HUD.dc.html` mid-generation
  (Opus 4.8, medium effort) when the connection dropped. Claude Design persists
  project state server-side, so this project is still there — worth opening
  directly in a normal (human-driven) browser session to see whatever it
  finished, and/or resuming the generation.
- Image upload into that page did **not** work in this environment: `file_upload`
  now rejects host filesystem paths outright (the tool description is stale —
  it still describes path-based upload as supported); `upload_image` against a
  screenshot-of-the-PNG failed with "Unable to access message history to
  retrieve image"; and a page-JS `fetch()`-based workaround to a local CORS
  server timed out (cross-origin fetch from `claude.ai` to `127.0.0.1` never
  resolved — likely blocked by Private Network Access / CSP, not a code bug).
  So the brief that went in was a **precise text description** of the current
  card (colors, sizes, layout, connector behavior) rather than the actual
  screenshots. This is worth fixing before the next design-research pass —
  probably means running this from a normal interactive session where images
  can be attached by hand, or finding out why `file_upload`'s path-based mode
  was deprecated and what replaced it.
- Also worth flagging: the two source screenshots don't match what the issue
  describes. `current-master-cropped.png` is the actual HUD "show all
  sessions" master view (cards, sparkline, dashed lineage connectors — matches
  the brief). `current-master.png` is a **Tasks panel** (a scheduler task list
  with pause/run buttons), unrelated to session cards. Only the cropped file
  was used as the current-state reference; the mislabeled one should probably
  be replaced or removed from `~/.agentwire/uploads/hud-design/`.

Given the external pass didn't finish, the rest of this doc is this session's
own synthesis — grounded in the current screenshot, the field catalogue, and
the design goals — offered as a starting point, not a validated outside
opinion. Treat it as a draft to react to, not a spec to build from blind.

## Current design (from `current-master-cropped.png`)

- Near-black background, ~176px cards (208px for the focused "self" card),
  laid out left-to-right in a horizontally-scrolling row.
- Card content: a status dot + session name on one line, then a tiny 4-5 bar
  sparkline (~10px tall) below it. That's it — most of the card's vertical and
  informational capacity is unused.
- Border color = family/lineage hue (green for self, orange and blue seen for
  two other families).
- Parent→child lineage drawn as blue dashed lines connecting cards, regardless
  of the families' own border colors — so the connector color doesn't match
  the family it's connecting for non-blue families.
- A "Sessions / Services" segmented toggle sits above the row.

The sparkline is the clearest problem: at 10px tall x ~5 bars it can't carry
real signal, and it's the only thing distinguishing an "active" card from an
identical-looking idle one besides the dot color.

## Recommendation

### Card anatomy (176px normal / 208px self), top to bottom

**1. Header row (~24px)** — status dot + name (as today), but add a small
right-aligned icon cluster for cheap-to-add signals: a mic glyph if
voice/PTT is available, a tiny numeric badge if there's an unread inbox count
or a pending routed prompt awaiting an answer. Keeps identity dense without
adding a row.

**2. Full-width activity strip (~28-32px, replaces the sparkline)** — this is
the main ask. Instead of decorative bars, a **pulse strip**: a thin horizontal
baseline that runs edge-to-edge, rendered like a heartbeat/EKG trace rather
than a bar chart (no axes, no chart-like affordances — it should read as a
*signal*, not data-viz).

- **Working / generating** → bright, tall, fast pulse in the family's hue
  (or neon green if you want "actively working" to always read the same
  color regardless of family — worth testing both).
- **Idle** → the line flattens to a slow, dim, low-amplitude wave. Still
  visibly "alive," just quiet.
- **Awaiting input (needs_input)** → amber/orange, with a distinct blinking
  marker at the strip's trailing edge — this is the state most worth making
  impossible to miss, since it's the one that means a human needs to act.
- **Stuck** → red, jagged/broken pattern, similar to a flatline-with-alarm
  visual metaphor.

Because real per-session activity history isn't plumbed yet (`derive→plumb`
in the field catalogue), this can ship now as a live-driven waveform (reacts
instantly to the current `activityStates` value, with a short client-side
decay trail so it still *looks* like a trace rather than a binary flip) and
upgrade to true history later without changing the visual language or
needing a redesign.

**3. Footer (optional, only for cards with something to say, ~16px or a
bare 1-2px edge)** — two cheap additions:
- A **context-remaining hairline**: a 1-2px strip along the card's bottom
  border that fills as context is consumed. Costs no vertical space (it's the
  border, not a row) and only applies to agent sessions where `remaining_pct`
  is available.
- **Worktree-only glyphs**: a small dirty/clean dot and `↑n`/`↓n`
  ahead/behind indicator, inline, for worktree sessions specifically. Skip
  entirely for non-worktree cards rather than reserving dead space.

### Fields worth surfacing beyond today (name + dot)

Given the "no dashboards" constraint, the short list that earns its place:
1. **Activity/wire state** → the pulse strip itself (replaces the sparkline
   1:1, so it's not "new" clutter, just a better encoding of what's already
   shown).
2. **Context remaining %** → bottom-edge hairline, agent sessions only.
3. **Git dirty/ahead-behind** → worktree cards only, inline glyphs.
4. **Unread inbox / pending prompt** → small badge, only rendered when > 0 —
   never takes space when there's nothing to report.

Everything else in the catalogue (model name, posture, roles[], PR/issue
linkage, etc.) is better suited to a hover/expand state or a drill-down view
than the compact card itself — surfacing it inline would cross into
"dashboard" territory the design goals explicitly warn against.

### Lineage & connectors

- Keep the family-hue border — it already does the job of grouping related
  cards.
- Tint the dashed parent→child connector line to match the family hue
  instead of always rendering blue. Today a blue connector under an orange
  family card reads as a mismatched, unrelated accent color.
- Route connectors behind the cards (lower z-index) so they don't visually
  compete with the new activity strip.

### Brand / glass carry-through

- Card background stays near-black with the frosted-glass blur already in
  place.
- Border: 1px in the family hue, upgrading to ~2px + a soft glow (box-shadow
  in the same hue) for the focused/self card — reuse the existing "self gets
  208px + emphasis" pattern rather than inventing a new one.
- The pulse strip is the one place worth spending a "neon" glow (subtle
  box-shadow/blur in the strip's color) — it's small and content-driven, so
  it won't tip the whole shade into looking gaudy the way a glowing border on
  every card would.

## Suggested next step

Re-run the `claude.ai/design` pass from a session where image upload actually
works (or attach the two screenshots by hand in an interactive browser), using
the design brief below as the starting prompt — it's already scoped to ask for
concrete layout specifics and a full-width activity visualization. Compare its
output against this draft rather than starting blind.

<details>
<summary>Design brief submitted to Claude Design (for reproducibility)</summary>

The full text brief (current-design description, design goals, full field
catalogue, and the ask) that was pasted into the `claude.ai/design` session
is preserved as a "Pasted text (50 lines)" attachment on the
`AgentWire Session Cards` project linked above — reopen that project to see
it verbatim rather than duplicating it here.

</details>
