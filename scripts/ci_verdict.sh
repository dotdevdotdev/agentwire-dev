#!/bin/bash
# ci_verdict <pr-number>
#
# One command that implements the repo's CI-provenance rules as tool behaviour
# instead of reviewer discipline. Read-only; queries GitHub only. Built from
# the thirteen instrument defects catalogued on #945 — each property below
# names the defect it closes.
#
#   * FULL 40-char SHA, derived here from the PR — never typed, never a short
#     SHA. An interface that DERIVES the identifier inherits none of the ways
#     a hand-held identifier can be wrong.
#   * SERVER-SIDE filter (`--commit <sha>`) — client-side filtering over a
#     repo-scoped page inherits the pagination defect, and the partial is a
#     knife-edge, not a region: a page boundary inside the run cluster returns
#     1-of-3 looking like a complete answer. Server-side is immune to CHURN
#     (not to truncation; moot at a handful of runs per SHA).
#   * NON-EMPTY refusal — an empty set is the most stable set there is, and a
#     zero is never evidence of a cause. Retention aging, the post-push
#     scheduling window, a non-trigger-target commit, a held fork PR, and a
#     bad query are all byte-identical to "checks did not run". Refuses with
#     the differential rather than classifying.
#   * NAME-BASED FLOOR, not a count — pytest, ruff and security have no path
#     filter, so every PR expects all three BY NAME; path-filtered workflows
#     (tts-smoke) can only ADD, and the addition is derived from the PR's own
#     changed paths, never from the runs observed (an expected set derived
#     from the result being checked proves nothing). A count cannot see a
#     workflow that never started, because the missing thing is not a quantity.
#   * steps==0 DISCRIMINATOR — a run-level `failure` can be a job that never
#     ran a step (queued 15 min, cancelled). `conclusion != success` with
#     `steps == 0` is "never started", whatever the conclusion string says,
#     and without depending on provider wording ("Set up job" etc.).
#   * ATTEMPT per run — GREEN ON ATTEMPT N, never "no red": a re-run erases
#     the red it replaces, so attempt-1's failing step is fetched and shown.
#   * PRINTS names and counts unconditionally, so a human can notice what the
#     tool cannot.
#   * No exit status is read through a pipe, and no command substitution is
#     trusted for control flow without checking the substitution's own result
#     — $(cmd) discards the exit code, so a tool that prints something
#     usable-looking on failure becomes a silent garbage source.
#
# Exit: 0 green · 1 red / floor violation / refusal · 2 pending — do not conclude
set -u

PR="${1:?usage: ci_verdict <pr-number>}"
REPO_SLUG="${GH_REPO:-dotdevdotdev/agentwire-dev}"

# The always-valid floor: every workflow on main with NO path filter. Derived
# from .github/workflows/*.yml triggers, not from any observed run set — keep
# it in sync with the workflows, not with what a PR happened to produce.
FLOOR_NAMES=(pytest ruff security)
# Path-filtered workflows and the filters that add them to the expected set.
TTS_SMOKE_PATHS='^(pyproject\.toml|uv\.lock|agentwire/tts/|\.github/workflows/tts-smoke\.yml)'

PRJSON=$(gh pr view "$PR" -R "$REPO_SLUG" --json headRefOid,isCrossRepository,state 2>/dev/null)
[ -n "$PRJSON" ] || { echo "could not read PR #$PR on $REPO_SLUG — refusing to query"; exit 1; }
SHA=$(printf '%s' "$PRJSON" | jq -r .headRefOid)
CROSS=$(printf '%s' "$PRJSON" | jq -r .isCrossRepository)
[ ${#SHA} -eq 40 ] || { echo "could not resolve a full 40-char head SHA (got '${SHA}')"; exit 1; }
echo "PR #$PR  head $SHA  cross-repo=$CROSS"

# Expected set: the unconditional floor, plus tts-smoke iff this PR's own
# changed paths reach its filter.
EXPECTED=("${FLOOR_NAMES[@]}")
if gh pr diff "$PR" -R "$REPO_SLUG" --name-only 2>/dev/null | grep -qE "$TTS_SMOKE_PATHS"; then
  EXPECTED+=(tts-smoke)
fi
echo "expected workflows (by name, from triggers): ${EXPECTED[*]}"

RUNS=$(gh run list -R "$REPO_SLUG" --limit 60 --commit "$SHA" \
        --json databaseId,name,status,conclusion,attempt --jq '.')
N=$(printf '%s' "$RUNS" | jq 'length')
echo "runs at this SHA: $N"

if [ "$N" -eq 0 ]; then
  echo "ZERO runs — REFUSING to classify. Emptiness is never evidence of a cause."
  echo "  Indistinguishable at this level:"
  echo "    - polled inside the post-push scheduling window (seconds; poll again)"
  echo "    - run records aged out of retention (old SHA — 0 runs means NOTHING there)"
  echo "    - the SHA is not a trigger target (intermediate commit of a multi-commit PR)"
  if [ "$CROSS" = "true" ]; then
    echo "    - CROSS-REPO PR: workflows may be held awaiting maintainer approval."
    echo "      The remedy is approval, not a re-run — a re-run of nothing does nothing."
  fi
  echo "    - a bad query (which would impersonate 'checks did not run')"
  exit 1
fi

printf "\n%-12s %-10s %-9s %-12s %s\n" STATUS CONCL ATTEMPT RUN NAME
printf '%s' "$RUNS" | jq -r '.[] | [.status,(.conclusion//"-"),.attempt,.databaseId,.name] | @tsv' |
while IFS=$'\t' read -r st cc at id nm; do
  printf "%-12s %-10s %-9s %-12s %s\n" "$st" "$cc" "$at" "$id" "$nm"
done

# NAME floor — detects a workflow that never started, which no count can.
MISSING=()
for want in "${EXPECTED[@]}"; do
  FOUND=$(printf '%s' "$RUNS" | jq --arg n "$want" '[.[] | select(.name==$n)] | length')
  [ "$FOUND" -gt 0 ] || MISSING+=("$want")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo
  echo "FLOOR VIOLATION: expected workflow(s) NEVER STARTED at this SHA: ${MISSING[*]}"
  echo "  Not pending, not red — absent. Do NOT conclude green from what is present."
  exit 1
fi

# Re-run archaeology: a red that was re-run away still gets classified.
echo
printf '%s' "$RUNS" | jq -r '.[] | select(.attempt > 1) | [.databaseId,.attempt,.name] | @tsv' |
while IFS=$'\t' read -r id at nm; do
  [ -z "$id" ] && continue
  echo "ATTEMPT $at on '$nm' — a red was re-run away. Classifying attempt 1:"
  c1=$(gh run view "$id" -R "$REPO_SLUG" --attempt 1 --json conclusion --jq .conclusion 2>/dev/null)
  step=$(gh run view "$id" -R "$REPO_SLUG" --attempt 1 --log-failed 2>/dev/null |
         awk -F'\t' 'NF>1{print $2}' | sort -u | head -3 | paste -sd, -)
  echo "    attempt 1: ${c1:-?}   failing step(s): ${step:-<none captured>}"
done

# steps==0 discriminator on every non-green run at the CURRENT attempt:
# separates "never ran a step" (provider/runner — category 3) from a real
# failure, independent of whether the conclusion reads failure or cancelled.
NEVER_STARTED=0
REAL_RED=0
echo
echo "jobs of non-green runs (steps==0 means the job NEVER STARTED):"
while IFS=$'\t' read -r id nm; do
  [ -z "$id" ] && continue
  JOBS=$(gh api "repos/$REPO_SLUG/actions/runs/$id/jobs?per_page=100" \
          --jq '[.jobs[] | {name, conclusion: (.conclusion//"pending"), steps: (.steps|length)}]' 2>/dev/null)
  [ -n "$JOBS" ] || { echo "    $nm/$id: could not read jobs — classify by hand"; REAL_RED=$((REAL_RED+1)); continue; }
  while IFS=$'\t' read -r jn jc js; do
    [ -z "$jn" ] && continue
    echo "    $nm / $jn: $jc  steps=$js"
    if [ "$jc" != "success" ] && [ "$jc" != "skipped" ] && [ "$jc" != "pending" ]; then
      if [ "$js" -eq 0 ]; then NEVER_STARTED=$((NEVER_STARTED+1)); else REAL_RED=$((REAL_RED+1)); fi
    fi
  done < <(printf '%s' "$JOBS" | jq -r '.[] | [.name,.conclusion,.steps] | @tsv')
done < <(printf '%s' "$RUNS" | jq -r '.[] | select(.status=="completed" and .conclusion!="success" and .conclusion!="skipped") | [.databaseId,.name] | @tsv')

NONTERM=$(printf '%s' "$RUNS" | jq '[.[] | select(.status!="completed")] | length')
# A pending run's conclusion is the EMPTY STRING, not null — excluding only
# null counts every pending run as a failure (measured on a live PR; a
# finished-PR-only validation cannot see it).
BAD=$(printf '%s' "$RUNS" | jq '[.[] | select((.conclusion//"") != "" and .conclusion!="success" and .conclusion!="skipped")] | length')
MAXATT=$(printf '%s' "$RUNS" | jq '[.[].attempt] | max')

echo
echo "VERDICT: $N runs · expected by name: ${EXPECTED[*]} (all present) · $NONTERM not terminal · $BAD non-success · max attempt $MAXATT"
if [ "$NONTERM" -gt 0 ]; then
  echo "  PENDING — do not conclude."
  exit 2
fi
if [ "$BAD" -gt 0 ]; then
  if [ "$REAL_RED" -eq 0 ] && [ "$NEVER_STARTED" -gt 0 ]; then
    echo "  RED-SHAPED, but every failing job has steps=0: nothing executed."
    echo "  Provenance category 3 IF supersession is excluded — check by hand:"
    echo "    no concurrency block in the workflow files, and the branch tip unmoved."
    echo "  No signal about the code either direction; the remedy is a re-run."
  else
    echo "  RED — at least one job ran steps and did not succeed. Classify before deciding."
  fi
  exit 1
fi
if [ "$MAXATT" -gt 1 ]; then
  echo "  GREEN ON ATTEMPT $MAXATT (not 'no red' — see the attempt-1 classification above)."
else
  echo "  GREEN ON ATTEMPT 1 — no red on any attempt at this SHA."
fi
exit 0
