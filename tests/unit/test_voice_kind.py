"""Slice 1b: ``voice`` is a first-class message kind (#985).

The only voice-layer change that alters behaviour for sessions with nothing to
do with voice — which is why it is its own reviewable diff, and why these tests
are pointed at the blast radius *outside* the voice layer.

Three things are asserted here and nowhere else:

1. **The owner's ruling, as data** (2026-08-10) — ``voice`` is ACTIVE (never
   passive, so it drives a session exactly as typing at it would) and IS in
   ``ESCALATE_KINDS`` (a dead-lettered voice message emails the owner, because
   the owner spoke it and walked away). And the distinction the ruling is most
   easily misread as: escalatable is **not** the interrupt tier. ``escalation``
   remains the only kind that pre-empts.

2. **One derivation, not four literals.** ``doctor``, ``worktree --list`` and
   ``worktree --watch`` each carried a hand-written ``("done", "escalation")``
   tuple. With ``voice`` added to ``ESCALATE_KINDS``, a missed copy makes a
   dead-lettered voice message email the owner on one path and vanish on
   another. :func:`inbox.load_bearing` is the one implementation; the
   set-equality assertion is what stops a future kind escaping it.

3. **The ``_cohort_held`` interaction**, which is the non-obvious part: it
   filters by SENDER, not kind, so a brand-new kind silently inherits the hold
   while ``cohort.REPORT_KINDS`` (which does filter by kind) does not know it.
"""

import pytest

from agentwire import cohort, doctor_cli, inbox, session_cli
from agentwire.voice_layer import confirm, write_tools


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_ROOT", tmp_path / "inbox")
    monkeypatch.setattr(inbox, "EVENTS_FILE", tmp_path / "inbox-events.jsonl")
    monkeypatch.setattr(inbox, "live_sessions", lambda: None)
    monkeypatch.setattr(cohort, "COHORT_ROOT", tmp_path / "cohorts")
    monkeypatch.setattr(cohort, "EVENTS_FILE", tmp_path / "cohort-events.jsonl")
    monkeypatch.setattr(cohort, "session_exists", lambda s: True)
    return tmp_path


def _corpse(session: str, kind: str, tag: str = "x") -> inbox.Message:
    """A dead-lettered message of *kind*, written straight into ``dead/``."""
    msg = inbox.Message(
        id=f"1700000000000-{kind}{tag}", sender="buddy", to=session, kind=kind,
        text=f"{kind} corpse", ts=1700000000000, attempts=inbox.MAX_ATTEMPTS,
        reason="box_not_empty", dead_ts=1700000000001,
    )
    inbox._write_message(inbox.dead_dir(session) / f"{msg.id}.json", msg)
    return msg


# =============================================================================
# 1. The ruling, as data
# =============================================================================


class TestTheRuling:
    def test_voice_is_a_kind(self):
        assert "voice" in inbox.KINDS

    def test_voice_is_active_not_passive(self):
        """A voice message IS the owner talking to a session through the buddy;
        it must drive the session exactly as typing at it would. Passive would
        be a behaviour REDUCTION versus the `<voice>` body prefix it replaces."""
        assert inbox.PASSIVE_KINDS == ("ingest",)
        assert inbox.is_passive("voice") is False

    def test_voice_is_escalatable(self):
        """The owner spoke it and walked away. Screenless, a silent dead-letter
        is unrecoverable — there is no screen on which to notice the graveyard."""
        assert "voice" in inbox.ESCALATE_KINDS

    def test_escalatable_is_not_the_interrupt_tier(self):
        """The distinction the ruling is most easily misread as.

        ``ESCALATE_KINDS`` governs dead-letter escalation. The interrupt tier is
        a separate, one-member set: only ``escalation`` pre-empts. Two producers
        key on it and neither may widen to ``voice`` — the fleet's dead-letter
        alert promotion, and the buddy client's spoken "Heads up —" prefix."""
        import inspect

        promotion = inspect.getsource(inbox._alert_dead_letters)
        assert 'm.kind == "escalation"' in promotion
        assert '"voice"' not in promotion

        from agentwire.voice_layer import client

        urgent = [
            line for line in inspect.getsource(client).splitlines()
            if "function isUrgent" in line
        ]
        assert urgent and 'm.kind === "escalation"' in urgent[0]
        assert "voice" not in urgent[0]

    def test_the_buddys_write_rides_the_voice_kind(self):
        """Attribution moves out of the body and into the slot that already
        drives behaviour. The property Slice 1 got from `request` — a
        dead-lettered buddy write emails the owner — survives the move."""
        assert write_tools.WRITE_KIND == "voice"
        assert write_tools.WRITE_KIND in inbox.ESCALATE_KINDS
        assert inbox.is_passive(write_tools.WRITE_KIND) is False

    def test_enqueue_accepts_voice_and_refuses_an_unknown_kind(self, isolate):
        (msg,) = inbox.enqueue("orchestrator", "hello", kind="voice", sender="buddy")
        assert msg.kind == "voice"
        with pytest.raises(ValueError):
            inbox.enqueue("orchestrator", "hello", kind="whisper", sender="buddy")

    def test_the_rendered_line_names_the_kind(self, isolate):
        """The slot attribution replacing the body prefix. `· voice` is what a
        recipient reads instead of a `<voice>` tag inside the text."""
        (msg,) = inbox.enqueue("orchestrator", "restart the portal",
                               kind="voice", sender="buddy")
        assert msg.render().startswith("[MSG from buddy · voice] restart the portal")


# =============================================================================
# 2. One derivation, not four literals
# =============================================================================


class TestLoadBearingIsDerivedOnce:
    def test_the_filter_is_set_equal_to_escalate_kinds(self, isolate):
        """The assertion that stops a future kind escaping the consolidation:
        feed one message of EVERY kind through the one filter and demand the
        survivors are exactly ``ESCALATE_KINDS``."""
        every = [
            inbox.Message(id=f"i-{k}", sender="s", to="t", kind=k, text=k, ts=1)
            for k in inbox.KINDS
        ]
        assert {m.kind for m in inbox.load_bearing(every)} == set(inbox.ESCALATE_KINDS)

    def test_no_consumer_carries_its_own_kind_literal(self):
        """The failure this issue exists to prevent, matched on the OPERATION.

        This test was spelling-keyed on review (#1004 nit 4): it grepped for
        ``kind in ("done"``, so re-inlining the same literal as
        ``("escalation", "done")`` — different order, identical defect — stayed
        green at both sites. A test named for catching a hand-written kind
        enumeration must catch one however it is spelled.

        So it walks the AST instead and flags ANY tuple/list/set literal of
        string constants that is a non-trivial subset of ``inbox.KINDS``,
        wherever it appears and in whatever order. The three SSOT assignments
        (``KINDS`` / ``PASSIVE_KINDS`` / ``ESCALATE_KINDS``) are the only
        permitted ones, exempted by assignment target rather than by content.
        """
        import ast
        import inspect

        ssot = {"KINDS", "PASSIVE_KINDS", "ESCALATE_KINDS"}
        kinds = set(inbox.KINDS)

        for module in (doctor_cli, session_cli, inbox):
            tree = ast.parse(inspect.getsource(module))
            exempt = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name) and target.id in ssot
            }
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    continue
                if id(node) in exempt:
                    continue
                values = [
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if len(values) != len(node.elts) or len(values) < 2:
                    continue
                assert not set(values) <= kinds, (
                    f"{module.__name__}:{node.lineno} hand-writes a kind "
                    f"enumeration {tuple(values)} — derive from "
                    f"inbox.ESCALATE_KINDS via inbox.load_bearing()"
                )

    def test_no_shipped_prose_carries_a_stale_kind_enumeration(self):
        """Nit 3, generalised: a fourth copy in a channel the model READS.

        The consolidation deleted the code literals and left the prose ones —
        ``agentwire/roles/agentwire.md`` and two ``SKILL.md`` files each spell
        out ``note|done|request|escalation``. That copy had already gone stale
        on ``ingest`` before ``voice`` existed, which is the same defect step 3
        exists to prevent, in the surface an agent actually consults.

        Prose cannot derive from the enum, so it gets the next best thing: any
        pipe-separated run naming two or more kinds must name them ALL.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        kinds = set(inbox.KINDS)
        runs = re.compile(r"[A-Za-z*`]+(?:\|[A-Za-z*`]+)+")

        checked = 0
        for path in [*(root / "agentwire" / "roles").glob("*.md"),
                     *(root / ".claude" / "skills").rglob("SKILL.md")]:
            for run in runs.findall(path.read_text()):
                tokens = {tok.strip("*`") for tok in run.split("|")}
                named = tokens & kinds
                if len(named) < 2:
                    continue
                checked += 1
                assert named == kinds, (
                    f"{path.relative_to(root)} enumerates kinds but is missing "
                    f"{sorted(kinds - named)}: {run}"
                )
        assert checked, "found no kind enumeration to check — regex went stale"

    def test_doctor_reports_a_dead_lettered_voice_message(self, isolate, capsys):
        _corpse("orchestrator", "voice")
        doctor_cli._render_dead_letter_section()
        out = capsys.readouterr().out
        assert "[!!]" in out and "voice" in out

    def test_doctor_still_ignores_a_dead_lettered_note(self, isolate, capsys):
        """The must-fail control: if the filter had widened to "every kind",
        this test would pass for the wrong reason and prove nothing."""
        _corpse("orchestrator", "note")
        doctor_cli._render_dead_letter_section()
        out = capsys.readouterr().out
        assert "[!!]" not in out

    def test_worktree_list_badges_a_dead_lettered_voice_message(self, isolate):
        _corpse("proj-slice", "voice")
        rows = [{"session": "proj-slice"}]
        session_cli._attach_dead_reports(rows)
        assert [m["kind"] for m in rows[0]["dead_reports"]] == ["voice"]

    def test_worktree_list_ignores_a_dead_lettered_note(self, isolate):
        _corpse("proj-slice", "note")
        rows = [{"session": "proj-slice"}]
        session_cli._attach_dead_reports(rows)
        assert rows[0]["dead_reports"] == []


# =============================================================================
# 3. The cohort interaction — held by SENDER, harvested by KIND
# =============================================================================


class TestCohortInteraction:
    """``_cohort_held`` filters by sender, ``_harvest`` filters by kind. A new
    kind lands on the wrong side of that seam by default, so it is pinned."""

    def test_a_voice_message_from_a_pending_child_is_held_by_sender(self, isolate):
        cohort.enroll("parent", "buddy", task="t")
        (msg,) = inbox.enqueue("parent", "hi", kind="voice", sender="buddy")
        assert inbox._cohort_held("parent", [msg]) == [msg]

    def test_a_voice_message_from_a_non_child_is_never_held(self, isolate):
        cohort.enroll("parent", "worker-a", task="t")
        (msg,) = inbox.enqueue("parent", "hi", kind="voice", sender="buddy")
        assert inbox._cohort_held("parent", [msg]) == []

    def test_a_held_voice_message_is_not_harvested_as_a_report(self, isolate):
        """The seam stated out loud: a voice message is the OWNER speaking, not
        a child's report-back, so it is deliberately absent from
        ``REPORT_KINDS``. Held-but-not-harvested is not lost — it stays pending
        and delivers once the cohort resolves, the same shape ``ingest`` has."""
        assert "voice" not in cohort.REPORT_KINDS
        cohort.enroll("parent", "buddy", task="t")
        inbox.enqueue("parent", "hi", kind="voice", sender="buddy")
        inbox.enqueue("parent", "PR up", kind="done", sender="buddy")
        harvested = cohort._harvest("parent")
        assert [m.kind for m in harvested["buddy"]] == ["done"]
        assert {m.kind for m in inbox.list_messages("parent")} == {"voice", "done"}

    def test_the_hold_releases_when_the_cohort_resolves(self, isolate):
        cohort.enroll("parent", "buddy", task="t")
        (msg,) = inbox.enqueue("parent", "hi", kind="voice", sender="buddy")
        assert inbox._cohort_held("parent", [msg]) == [msg]
        cohort.discard("parent")
        assert inbox._cohort_held("parent", [msg]) == []


# =============================================================================
# 4. Attribution left the body — and took its safety property with it
# =============================================================================


class TestAttributionLeftTheBody:
    def test_the_body_no_longer_carries_a_marker(self):
        body = confirm.render_body("restart the portal", "confirm tango", "a1b2c3")
        assert "<voice>" not in body
        assert body.startswith("restart the portal")

    def test_a_leading_dash_is_still_impossible(self):
        """The marker used to guarantee this incidentally — the body could not
        start with a dash because it started with ``<voice>``. ``instruction``
        is model-supplied and reaches the CLI as a positional, so a body
        leading with ``-`` is parsed as a FLAG; this repo has shipped exactly
        that bug twice. Removing the marker re-opens the hole unless the
        guarantee is made explicit, which is what ``_lead_safe`` does."""
        body = confirm.render_body("--force a restart", "confirm tango", "a1b2c3")
        assert not body.startswith("-")
        assert "force a restart" in body

    def test_a_split_dash_run_is_stripped_too(self):
        """The reviewer's case on #1004, and it was DATA LOSS, not cosmetics.

        The first fix stripped only the leading dash RUN (``lstrip("-")`` before
        ``.strip()``), so ``"- - force"`` kept its second dash and tripped
        ``render_body``'s guard. That guard fires inside ``build_argv()``, which
        ``ConfirmSpine.confirm`` calls AFTER ``_proposals.pop()`` and OUTSIDE the
        runner's ``try`` — so the proposal is already consumed and the approving
        utterance already spent. Screenless, the owner loses the message
        entirely, with no retry and nothing on screen saying why.
        """
        for instruction in (
            "- - force a restart",
            "-\t-x",
            "- -x",
            "--- ---",
            "  --  --  restart the portal",
        ):
            body = confirm.render_body(instruction, "", "a1b2c3")
            assert not body.startswith("-"), instruction

    def test_lead_safe_is_total_over_adversarial_prefixes(self):
        """Totality is the point, not a longer list of cases.

        ``_lead_safe`` has to be TOTAL because the alternative — a guard that
        raises here — destroys the message. Swept over every prefix built from
        dash/space/tab up to length 4, against a real instruction and against
        nothing but the prefix.
        """
        from itertools import product

        for n in range(5):
            for combo in product("- \t", repeat=n):
                prefix = "".join(combo)
                for tail in ("restart the portal", ""):
                    body = confirm.render_body(prefix + tail, "", "a1b2c3")
                    assert not body.startswith("-"), repr(prefix + tail)
                    assert body.endswith("#a1b2c3"), repr(prefix + tail)

    def test_the_guarantee_survives_assertions_being_disabled(self):
        """The second-order half, and the one that matters more than the regex.

        The original guard was an ``assert``, which ``python -O`` compiles out —
        so in optimised mode a flag-shaped body shipped silently, which is the
        bug the guard was named for. A guarantee that evaporates under a
        standard interpreter flag is not a guarantee. Run in a real ``-O``
        subprocess, because that is the only way to observe it.
        """
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-O", "-c",
             "from agentwire.voice_layer import confirm;"
             "print(confirm.render_body('- - force a restart', '', 'a1b2c3'))"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not out.startswith("-"), out
        assert out.endswith("#a1b2c3")

    def test_an_all_dash_instruction_still_renders_a_safe_body(self):
        body = confirm.render_body("---", "", "a1b2c3")
        assert not body.startswith("-")
        assert body.endswith("#a1b2c3")

    def test_the_freed_budget_is_accounted_for_not_silently_consumed(self):
        """#981 finding 6: the reply nudge competes for the same budget. The
        marker's 8 chars come back to the BODY and the kind slot shrinks the
        RENDERED line, so the worst case must have moved DOWN, not up."""
        body = confirm.render_body(
            "x" * confirm.MAX_RENDERED_INSTRUCTION_CHARS,
            "y" * confirm.MAX_UTTERANCE_CHARS,
            "a1b2c3",
            reply_to="b" * 40,
        )
        assert len(body) <= confirm.MAX_BODY_CHARS
        worst = inbox.Message(
            id="1700000000000-abcdef", sender="w" * 32, to="t",
            kind=write_tools.WRITE_KIND, text="z" * confirm.MAX_BODY_CHARS, ts=1,
        ).render()
        assert len(worst) == confirm.WORST_RENDERED_LINE_CHARS
        assert len(worst) < confirm.MEASURED_STUCK_LIMIT_CHARS

    def test_the_nudge_now_fits_where_it_previously_did_not(self):
        """The measurable half of "re-measure the caps": 8 body chars came
        back, so a body that used to lose the droppable nudge now keeps it."""
        # A witness in the 8-char window the marker's removal opened: a
        # 120-char instruction with a full-length utterance renders at 293, so
        # the nudge rides. The Slice 1 body was this plus `<voice> ` — 301,
        # over the cap, so the nudge was dropped whole.
        body = confirm.render_body(
            "x" * 120, "y" * confirm.MAX_UTTERANCE_CHARS, "a1b2c3", reply_to="buddy"
        )
        assert confirm.reply_nudge("buddy") in body
        assert len(body) <= confirm.MAX_BODY_CHARS
        assert len(f"<voice> {body}") > confirm.MAX_BODY_CHARS


# =============================================================================
# 5. The role text the move makes false
# =============================================================================


class TestRoleTextStaysTrue:
    def test_no_role_prompt_still_teaches_the_body_prefix(self):
        from pathlib import Path

        roles = Path(__file__).resolve().parents[2] / "agentwire" / "roles"
        for name in ("worker.md", "worker-worktree.md", "orchestrator.md"):
            text = (roles / name).read_text()
            assert "<voice>" not in text, name
            assert "`voice`" in text, name
