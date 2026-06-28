# Large Parallel Refactors

> Living document. Update this, don't create new versions.

A playbook for splitting one very large file — or otherwise reshaping a big chunk of a codebase — by fanning the work out across **parallel worktree sessions** that PR back into a shared feature branch. It exists because several lessons from the #495 CLI-monolith extraction (12,854-line `agentwire/__main__.py` → 26 `*_cli.py` modules + `core.py`, PRs #529–#538) proved load-bearing and reusable, and re-deriving them each time is expensive.

**When this applies:** you're about to extract / move / mechanically transform many functions out of one large file (or across many files) and want to parallelize for wall-clock speed. One orchestrator session drives N worker worktrees, each owning a disjoint slice, each opening a draft PR into a feature branch the orchestrator controls.

**When it does NOT:** a small refactor (do it inline), or work where the slices touch genuinely independent files (no shared file → no positional conflict → just fan out and merge in any order). The whole playbook is about the failure mode that appears when **multiple branches edit the same large file**.

---

## The core hazard: positional interleaving

The instinct is "the groups are logically disjoint (panes vs sessions vs scheduler), so their branches won't conflict." **This is wrong for a shared file.** Functions don't live in logical space — they live at **line positions**. When every branch deletes its own functions out of the *same* monolith, the deletions interleave:

```
__main__.py (original)          group A removes a*    group B removes b*
  cmd_a1   ──┐                    (lines shift up)      (lines shift up)
  cmd_b1     │  A's and B's        cmd_b1                cmd_a1
  cmd_a2     │  functions are      cmd_b2                cmd_a2
  cmd_b2     │  INTERLEAVED by     cmd_b3                cmd_a3
  cmd_a3     │  line position
  cmd_b3   ──┘
```

Once **group A merges**, the feature branch tip no longer has A's functions — so the lines around B's functions have all moved. Group B's branch was cut from the *old* positions. `git merge`/`rebase` now sees large **adjacency conflicts** spanning the blocks next to every edit — not just the registry/import wiring lines you'd expect. The conflict surface is proportional to how interleaved the groups were, which for a real monolith is "completely."

**Parallel-regenerating all branches onto the same base does NOT save you.** We tried it: regenerate every remaining branch onto the post-merge tip *simultaneously*. They still conflict with **each other**, because disjoint function *identities* still occupy interleaved *positions*. There is no base on which two interleaved-deletion branches merge cleanly. The only thing that converges is doing it **one at a time**.

---

## The fix: regenerate-against-fresh-base, merged sequentially

Merge the branches **one at a time**. After each merge, the next branch **regenerates** its change against the new tip rather than trying to merge its stale branch in. Per branch:

```bash
# base = the feature branch's CURRENT tip (after the previous merge)
git fetch origin <feature-branch>

# 1. Take the shared file fresh from the base (discards this branch's stale copy)
git checkout origin/<feature-branch> -- agentwire/__main__.py

# 2. Re-apply ONLY this group's own removal. This is now CLEAN: with the other
#    already-merged groups gone, this group's functions are contiguous, so the
#    deletion is a simple block-removal with no interleaving left to fight.
#    (Re-run the extraction edit, or cherry-pick just the move, against the fresh file.)

# 3. Collapse to ONE clean commit on the current base.
git reset --soft origin/<feature-branch>
git commit -m "refactor(cli): extract <group> to <group>_cli.py (#NNN)"
git push --force-with-lease
```

**Why `reset --soft` and not just `git checkout -- file`:** a plain `git checkout origin/<base> -- file` gives the *right content* but leaves the branch's **merge-base stale** — GitHub still computes the diff against the old fork point, so the PR shows as CONFLICTING and the diff is full of noise from the other groups' churn. `git reset --soft <base>` re-parents the branch onto the current tip and collapses everything to a single net-diff commit, so the PR is clean and **cleanly mergeable**.

**Merge order:** pick any order, merge one, then regenerate the *next* branch onto the new tip. Repeat. The last branch regenerates against a base where every other group is already extracted — its removal is trivially contiguous.

---

## Foundation-first (Phase 0 / Phase 0.5)

Parallel agents collide on **shared helpers**. If `_run_remote` lives in the monolith and three groups all call it, three branches will all try to move or reference it and stomp each other. So before any fan-out:

- **Phase 0 — establish the seam.** Extract the stateless shared helpers into a `core.py`-style module, and convert dispatch to a registrar pattern (`register_<domain>_parser(subparsers)` + `set_defaults(func=…)` collected in a list-loop) so each group wires itself in via **one append**, not an edit to a shared if/elif chain. Finish one small group end-to-end (here: `limits_cli`) to prove the pattern before scaling it.
- **Phase 0.5 — sweep cross-group helpers.** The initial foundation misses helpers that *only become* shared once you look across groups. Do a dedicated pass: a helper moves to `core` **iff it's called by commands in 2+ groups**; single-group helpers move *with* their domain in the waves. (#495 needed ~14 more helpers swept here — `_run_remote` alone had 29 callers — that Phase 0 didn't catch.)

Only then fan out the waves. Each group's worker owns a disjoint, self-contained slice and references shared code via `core` (or **function-local deferred imports** between sibling modules, `from . import x_cli` inside the caller, to neutralize import cycles regardless of how you partitioned).

---

## Verification discipline

**Run the FULL suite, never a subset.** `uv run --extra dev pytest tests/unit tests/integration`. Integration tests reach into moved symbols by module attribute — `from agentwire import __main__ as m; m.cmd_foo` — and when a symbol moves, its **patch target must be repointed** to the module the system-under-test now resolves it from. Scoping a worker's verify to `tests/unit` silently passes while integration patches break; that reddened a #495 PR's CI *after* it looked green locally. Test changes in a pure extraction should be **only** patch-target updates — no assertion changes.

**Verify coupling claims with `grep`, not a recon model's prose.** When planning the partition, a sub-agent (recon) hallucinated a large cross-domain "call mesh" that would have forced a much more conservative split. A deterministic grep showed the *real* graph was **5 inter-command edges**. Trust the grep:

```bash
# inter-command calls = real coupling the partition must respect
grep -nE "\bcmd_[a-z_]+\(" agentwire/__main__.py | grep -vE "set_defaults|^def "
```

The partition rides on this graph; derive it mechanically, don't take a model's word for the shape of the code.

---

## Orchestration hygiene

What worked for driving the fleet:

- **One worktree session per group**, each PR'ing into the shared **feature branch** (never `main`), so the orchestrator stays in control of integration. Dispatch: `agentwire worktree <group> --base <feature-branch> -p <repo> --prompt "…"`. The `worktree-session` role (auto-injected by the verb) already encodes isolation + draft-PR + notify-back, so prompts only carry the task.
- **Independent review gate per PR**, all mechanical: ① diff scope is only the expected files; ② **dangling-ref grep** — every moved name is absent from the monolith except its import + registrar line; ③ **no-redefine grep** — `core` helpers are *imported*, not copied into the new module; ④ CI green.
- **Backstop with polls, not notifications.** Worktree-completion notifications are unreliable (a polite report-back can dead-letter against a busy orchestrator — see [messaging](../sessions/messaging.md) and #523). Run a background `gh pr …` poll for each expected PR rather than waiting to be told.
- **`send --wait-ready --verify` "could not be verified" is a false negative.** It frequently reports failure on a delivery that landed. Confirm via `agentwire output` before re-sending — a blind retry double-drives the session.
- **Merges need `--admin`.** Branch protection counts advisory checks as required, so `gh pr merge N --squash --admin`. Mark `gh pr ready N` first if it's a draft (a re-push reverts a PR to draft).

---

## The shape, end to end

`Phase 0` (seam: `core.py` + registrar dispatch, prove on one small group) → `Phase 0.5` (cross-group helper sweep) → `Wave 1…N` (disjoint groups in parallel, each a worktree → draft PR into the feature branch) → merge **sequentially, regenerating each next branch onto the new tip** → `Phase 2` (any behavior-touching follow-ups, e.g. rewiring callers) → merge the feature branch to `main`.

#495 ran this as Phase 0 → 0.5 → Wave 1 (3 groups) → Wave 2 (4 groups) → Phase 2, landing `__main__.py` at **391 lines** (entry + `build_parser()` registrar-loop + `main()`, zero `cmd_*`) with the CLI surface verified identical.

---

## Checklist

- [ ] Shared file? If no shared file, skip this — just fan out and merge in any order.
- [ ] Phase 0: extract `core.py` + a registrar/dispatch seam; prove on one small group.
- [ ] Phase 0.5: sweep helpers called by 2+ groups into `core`; leave single-group helpers with their domain.
- [ ] Derive the real coupling graph with `grep`, not a recon model's prose.
- [ ] Fan out: one worktree session per disjoint group, draft PR into the feature branch.
- [ ] Per PR: diff-scope + dangling-ref grep + no-redefine grep + **full-suite** pytest.
- [ ] Merge **one at a time**; regenerate each next branch against the fresh base (`checkout -- file` → re-apply own removal → `reset --soft <base>` → one commit → force-push).
- [ ] Repoint moved-symbol patch targets in integration tests (no assertion changes).
- [ ] Backstop with `gh pr` polls; treat `--verify` "could not be verified" as a false negative.
