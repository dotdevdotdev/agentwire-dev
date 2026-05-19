"""Tests for ``agentwire.missions.eligibility``."""

from agentwire.missions.eligibility import (
    AGENT_READY_LABEL,
    extract_acceptance_criteria,
    has_agent_ready_label,
    is_eligible,
)
from agentwire.missions.github import Issue


def make_issue(
    *,
    number: int = 1,
    title: str = "T",
    body: str = "",
    labels: tuple[str, ...] = (),
    state: str = "OPEN",
) -> Issue:
    return Issue(number=number, title=title, body=body, labels=labels, state=state)


class TestHasAgentReadyLabel:
    def test_present(self):
        assert has_agent_ready_label(make_issue(labels=(AGENT_READY_LABEL,))) is True

    def test_missing(self):
        assert has_agent_ready_label(make_issue(labels=("other",))) is False

    def test_among_others(self):
        assert has_agent_ready_label(
            make_issue(labels=("feature:foo", AGENT_READY_LABEL, "area:bug"))
        ) is True


class TestExtractAcceptanceCriteria:
    def test_simple(self):
        body = "## Acceptance criteria\n- one\n- two\n"
        assert extract_acceptance_criteria(body) == ["one", "two"]

    def test_lowercase_header(self):
        body = "## acceptance criteria\n- one\n"
        assert extract_acceptance_criteria(body) == ["one"]

    def test_mixed_case_header(self):
        body = "## Acceptance Criteria\n- one\n"
        assert extract_acceptance_criteria(body) == ["one"]

    def test_star_and_plus_bullets(self):
        body = "## Acceptance criteria\n* star\n+ plus\n- dash\n"
        assert extract_acceptance_criteria(body) == ["star", "plus", "dash"]

    def test_stops_at_next_header(self):
        body = "## Acceptance criteria\n- one\n\n## Notes\n- not a criterion\n"
        assert extract_acceptance_criteria(body) == ["one"]

    def test_continues_through_paragraphs(self):
        body = "## Acceptance criteria\n\nSome preamble.\n\n- one\n- two\n"
        assert extract_acceptance_criteria(body) == ["one", "two"]

    def test_no_header_returns_none(self):
        assert extract_acceptance_criteria("## Goals\n- not it\n") is None

    def test_header_but_no_bullets_returns_none(self):
        assert extract_acceptance_criteria("## Acceptance criteria\n\nNo bullets here.\n") is None

    def test_empty_body(self):
        assert extract_acceptance_criteria("") is None

    def test_strips_bullet_marker(self):
        body = "## Acceptance criteria\n-   spaced bullet\n"
        assert extract_acceptance_criteria(body) == ["spaced bullet"]

    def test_only_first_section_counts(self):
        body = (
            "## Acceptance criteria\n"
            "- one\n"
            "\n"
            "## Acceptance criteria\n"
            "- duplicate header ignored\n"
        )
        # First section wins; bullets in the second (same-name) section are
        # consumed by the next-header stop.
        assert extract_acceptance_criteria(body) == ["one"]


class TestIsEligible:
    def test_eligible(self):
        issue = make_issue(
            labels=(AGENT_READY_LABEL,),
            body="## Acceptance criteria\n- do the thing\n",
        )
        ok, reason = is_eligible(issue)
        assert ok is True and reason == ""

    def test_not_eligible_missing_label(self):
        issue = make_issue(body="## Acceptance criteria\n- x\n")
        ok, reason = is_eligible(issue)
        assert ok is False and "label" in reason

    def test_not_eligible_no_criteria(self):
        issue = make_issue(labels=(AGENT_READY_LABEL,), body="just prose")
        ok, reason = is_eligible(issue)
        assert ok is False and "Acceptance criteria" in reason

    def test_not_eligible_closed(self):
        issue = make_issue(
            labels=(AGENT_READY_LABEL,),
            body="## Acceptance criteria\n- x\n",
            state="CLOSED",
        )
        ok, reason = is_eligible(issue)
        assert ok is False and "CLOSED" in reason
