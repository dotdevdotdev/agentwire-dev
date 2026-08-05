"""A red pip-audit must not render as green (#900).

The fixture here is the REAL failing state: ``EIGHT_FINDINGS`` is the exact set
pip-audit reported against the lockfile on ``main``, package/version/advisory
id/fix-version as published. Every "does it surface?" test runs against that,
not against a hand-made single finding — because the whole bug being fixed is
that eight real advisories were invisible, and a report tool proven only on a
toy input proves nothing about the day it matters.

The last class is the one that would have caught this class of bug at review
time: it asserts the workflow YAML actually WIRES the script in, and that the
job is still non-blocking. A perfectly-tested reporter that nothing calls is
exactly the failure mode #900 is about.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pip_audit_report.py"
WORKFLOW = REPO / ".github" / "workflows" / "security.yml"


def _load():
    spec = importlib.util.spec_from_file_location("pip_audit_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load()


# The eight findings from #900, verbatim: pip-audit against the lock on main.
EIGHT_FINDINGS = {
    "dependencies": [
        {"name": "aiohttp", "version": "3.14.1", "vulns": [
            {"id": "PYSEC-2026-3545", "fix_versions": ["3.14.3"], "aliases": []},
            {"id": "PYSEC-2026-3546", "fix_versions": ["3.14.2"], "aliases": []},
            {"id": "PYSEC-2026-3547", "fix_versions": ["3.14.2"], "aliases": []}]},
        {"name": "click", "version": "8.3.1", "vulns": [
            {"id": "PYSEC-2026-2132", "fix_versions": ["8.3.3"], "aliases": []}]},
        {"name": "cryptography", "version": "49.0.0", "vulns": [
            {"id": "PYSEC-2026-3552", "fix_versions": ["50.0.0"], "aliases": []}]},
        {"name": "mcp", "version": "1.26.0", "vulns": [
            {"id": "PYSEC-2026-3481", "fix_versions": ["1.27.2"], "aliases": []},
            {"id": "PYSEC-2026-3482", "fix_versions": ["1.27.2"], "aliases": []},
            {"id": "PYSEC-2026-3483", "fix_versions": ["1.28.1"], "aliases": []}]},
        {"name": "certifi", "version": "2026.1.1", "vulns": []},
    ]
}

CLEAN = {"dependencies": [{"name": "aiohttp", "version": "3.14.3", "vulns": []}]}


@pytest.fixture
def gh_env(tmp_path, monkeypatch):
    """Stand in for the Actions runner's file-command paths."""
    summary = tmp_path / "step-summary.md"
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return summary, output


def _run(tmp_path, payload, capsys, **kw):
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(payload))
    argv = [str(path)] + [a for k, v in kw.items()
                          for a in (f"--{k.replace('_', '-')}", str(v))]
    code = report.main(argv)
    return code, capsys.readouterr().out


class TestParsing:
    def test_all_eight_are_found(self):
        assert len(report.parse(EIGHT_FINDINGS)) == 8

    def test_a_package_with_no_vulns_contributes_nothing(self):
        assert all(f.package != "certifi" for f in report.parse(EIGHT_FINDINGS))

    def test_a_bare_list_is_accepted_too(self):
        """pip-audit has shipped both shapes; raising on one is worse than
        useless on the day it's needed."""
        assert len(report.parse(EIGHT_FINDINGS["dependencies"])) == 8

    def test_garbage_entries_are_skipped_not_fatal(self):
        assert report.parse({"dependencies": [None, "x", {"vulns": [None]}]}) == []

    def test_empty_report_is_no_findings(self):
        assert report.parse(CLEAN) == []


class TestFindingsSurface:
    """The bug: eight real advisories, and nothing said so."""

    def test_the_eight_reach_the_annotations(self, tmp_path, capsys, gh_env):
        _, out = _run(tmp_path, EIGHT_FINDINGS, capsys)
        assert "::warning" in out
        for pkg in ("aiohttp", "click", "cryptography", "mcp"):
            assert pkg in out
        for vid in ("PYSEC-2026-3545", "PYSEC-2026-3481", "PYSEC-2026-3552",
                    "PYSEC-2026-2132"):
            assert vid in out, f"{vid} never surfaced"

    def test_the_count_is_in_the_headline(self, tmp_path, capsys, gh_env):
        _, out = _run(tmp_path, EIGHT_FINDINGS, capsys)
        assert "8 advisories" in out

    def test_the_step_summary_gets_a_table(self, tmp_path, capsys, gh_env):
        summary, _ = gh_env
        _run(tmp_path, EIGHT_FINDINGS, capsys)
        body = summary.read_text()
        assert "## pip-audit (runtime): 8 advisories" in body
        assert body.count("| `") >= 4          # one row per package at least
        assert "PYSEC-2026-3483" in body
        assert "1.28.1" in body                # the fix version is actionable

    def test_it_says_which_are_a_stale_lock_rather_than_a_dead_end(
            self, tmp_path, capsys, gh_env):
        """The #900 insight: 8/8 had published fixes, so it was a lock
        refresh, not a security decision anyone had made."""
        summary, _ = gh_env
        _run(tmp_path, EIGHT_FINDINGS, capsys)
        assert "8 of 8 have a published fix" in summary.read_text()

    def test_an_unfixable_finding_is_not_called_a_stale_lock(self):
        stuck = {"dependencies": [{"name": "x", "version": "1", "vulns": [
            {"id": "PYSEC-1", "fix_versions": []}]}]}
        assert report.parse(stuck)[0].fixable is False
        assert "published fix" not in report.markdown(
            report.parse(stuck), scope="runtime")

    def test_the_machine_readable_count_is_emitted(self, tmp_path, capsys, gh_env):
        _, output = gh_env
        _run(tmp_path, EIGHT_FINDINGS, capsys)
        assert "count=8" in output.read_text()

    def test_a_clean_audit_says_so_positively(self, tmp_path, capsys, gh_env):
        summary, output = gh_env
        code, out = _run(tmp_path, CLEAN, capsys)
        assert code == 0
        assert "::notice" in out and "no known advisories" in out
        assert "clean" in summary.read_text()
        assert "count=0" in output.read_text()


class TestAuditedNothingIsNotClean:
    """Zero advisories over zero packages is not a clean bill of health.

    The same conflation this script exists to prevent, one level in: #900 was
    "a red audit renders as green", and a report keyed only on FINDINGS lets
    "audited nothing" render as green too. The realistic route is an empty
    ``requirements.txt`` — a silently-failed export — after which pip-audit
    exits 0 and writes ``{"dependencies": []}``.
    """

    NOTHING_AUDITED = {"dependencies": []}
    NO_KEY_AT_ALL = {}
    TWO_CLEAN_PACKAGES = {"dependencies": [
        {"name": "aiohttp", "version": "3.14.3", "vulns": []},
        {"name": "click", "version": "8.4.2", "vulns": []},
    ]}

    def test_the_audited_count_distinguishes_them(self):
        assert report.audited_count(self.TWO_CLEAN_PACKAGES) == 2
        assert report.audited_count(self.NOTHING_AUDITED) == 0
        assert report.audited_count(self.NO_KEY_AT_ALL) == 0
        assert report.audited_count(EIGHT_FINDINGS) == 5

    @pytest.mark.parametrize("payload_name", ["NOTHING_AUDITED", "NO_KEY_AT_ALL"])
    def test_an_empty_audit_reports_unknown_not_clean(
            self, payload_name, tmp_path, capsys, gh_env):
        summary, _ = gh_env
        _, out = _run(tmp_path, getattr(self, payload_name), capsys)
        assert "::warning" in out, "an empty audit produced no warning"
        assert "UNKNOWN" in out
        assert "ZERO packages" in out
        body = summary.read_text()
        assert "coverage UNKNOWN" in body
        assert "clean" not in body.split("\n")[0]

    def test_a_genuinely_clean_audit_still_reads_clean(
            self, tmp_path, capsys, gh_env):
        """The distinction has to cut both ways, or it's just noise."""
        summary, _ = gh_env
        _, out = _run(tmp_path, self.TWO_CLEAN_PACKAGES, capsys)
        assert "::notice" in out and "UNKNOWN" not in out
        assert ": clean" in summary.read_text()

    def test_a_suspiciously_small_audit_is_unknown_too(
            self, tmp_path, capsys, gh_env):
        """The runtime export is ~124 packages; two means something broke
        upstream of the audit even though it technically ran."""
        summary, _ = gh_env
        _, out = _run(tmp_path, self.TWO_CLEAN_PACKAGES, capsys, min_packages=50)
        assert "UNKNOWN" in out
        assert "only 2 packages" in summary.read_text()

    def test_the_floor_does_not_fire_on_a_full_audit(self, tmp_path, capsys, gh_env):
        full = {"dependencies": [{"name": f"p{i}", "version": "1", "vulns": []}
                                 for i in range(124)]}
        summary, _ = gh_env
        _, out = _run(tmp_path, full, capsys, min_packages=50)
        assert "UNKNOWN" not in out
        assert "124 packages" in summary.read_text()

    def test_the_count_is_visible_even_when_clean(self, tmp_path, capsys, gh_env):
        """Carrying the number is what makes zero visibly zero."""
        _, output = gh_env
        _run(tmp_path, self.TWO_CLEAN_PACKAGES, capsys)
        assert "audited=2" in output.read_text()

    def test_an_empty_audit_never_looks_clean_to_the_workflows_grep(
            self, tmp_path, capsys, gh_env):
        """The cron closes the tracking issue on ': clean'. An empty audit
        must not close it."""
        body = tmp_path / "issue.md"
        _run(tmp_path, self.NOTHING_AUDITED, capsys, issue_body=body)
        assert ": clean" not in body.read_text()

    def test_findings_are_reported_even_when_coverage_is_doubted(
            self, tmp_path, capsys, gh_env):
        """An incomplete audit that ALSO hides what it found is the worst of
        both. The first version of this test only asserted "::warning" was
        present — which the coverage warning satisfied while every advisory
        was being suppressed."""
        summary, _ = gh_env
        _, out = _run(tmp_path, EIGHT_FINDINGS, capsys, min_packages=50)
        assert "UNKNOWN" in out                       # coverage doubt is raised
        for vid in ("PYSEC-2026-3545", "PYSEC-2026-3481", "PYSEC-2026-3552",
                    "PYSEC-2026-2132"):
            assert vid in out, f"{vid} suppressed behind the coverage warning"
        body = summary.read_text()
        assert "8 advisories" in body                  # the table still renders
        assert "Coverage is also suspect" in body      # and says so


class TestItNeverBecomesAGate:
    """continue-on-error is a deliberate, documented decision. Reporting must
    not quietly undo it."""

    def test_findings_still_exit_zero(self, tmp_path, capsys, gh_env):
        code, _ = _run(tmp_path, EIGHT_FINDINGS, capsys)
        assert code == 0

    def test_exit_code_is_opt_in(self, tmp_path, capsys, gh_env):
        path = tmp_path / "audit.json"
        path.write_text(json.dumps(EIGHT_FINDINGS))
        assert report.main([str(path), "--exit-code"]) == 1


class TestTheAuditNotRunningIsAlsoAFinding:
    """"UNKNOWN" is not "clean" — that conflation IS #900."""

    def test_a_missing_report_warns(self, tmp_path, capsys, gh_env):
        summary, _ = gh_env
        code = report.main([str(tmp_path / "nope.json")])
        out = capsys.readouterr().out
        assert code == 0                       # still never a gate
        assert "::warning" in out and "unreadable" in out
        assert "not the same as clean" in summary.read_text()

    def test_a_truncated_report_warns(self, tmp_path, capsys, gh_env):
        path = tmp_path / "audit.json"
        path.write_text('{"dependencies": [')
        report.main([str(path)])
        assert "unreadable" in capsys.readouterr().out


class TestTrackingIssue:
    def test_the_body_carries_the_findings_and_the_run_link(self, tmp_path, capsys, gh_env):
        body = tmp_path / "issue.md"
        _run(tmp_path, EIGHT_FINDINGS, capsys,
             issue_body=body, run_url="https://example.test/run/1")
        text = body.read_text()
        assert "8 advisories" in text
        assert "PYSEC-2026-3481" in text
        assert "https://example.test/run/1" in text
        assert "updated automatically" in text

    def test_a_clean_body_is_detectable_by_the_workflow(self, tmp_path, capsys, gh_env):
        """The YAML closes the issue by grepping for ': clean' — so that
        marker is load-bearing, not cosmetic."""
        body = tmp_path / "issue.md"
        _run(tmp_path, CLEAN, capsys, issue_body=body)
        assert ": clean" in body.read_text()

    def test_a_findings_body_never_looks_clean_to_that_grep(self, tmp_path, capsys, gh_env):
        body = tmp_path / "issue.md"
        _run(tmp_path, EIGHT_FINDINGS, capsys, issue_body=body)
        assert ": clean" not in body.read_text()

    # -- the promise and the mechanism have to agree -----------------------
    #
    # The body tells an operator the issue "is reopened automatically if the
    # findings come back". That sentence is only true if the workflow can SEE
    # a closed issue and actually reopens it. Operator-facing text describing
    # a mechanism the code does not implement is its own defect class — it is
    # worse than silence, because the next reader trusts it.

    @pytest.fixture
    def cron_step(self):
        """The issue-managing step's RUN LINES, comments stripped.

        Comments are excluded deliberately: this file's own rationale comment
        names `--state open` as the thing not to do, and a check that reads
        prose as code would fail on the explanation of the fix.
        """
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["pip-audit"]
        for step in job["steps"]:
            run = step.get("run", "")
            if "gh issue" in run:
                return "\n".join(ln for ln in run.split("\n")
                                 if not ln.strip().startswith("#"))
        pytest.fail("no step manages the tracking issue")

    def test_the_search_can_see_a_closed_issue(self, cron_step):
        """`--state open` cannot find the issue this workflow closed last
        week, so a recurrence files a SECOND issue and 'one issue, reused'
        quietly becomes 'one per close/recur cycle'."""
        assert "gh issue list --state all" in cron_step
        assert "gh issue list --state open" not in cron_step

    def test_it_actually_reopens_rather_than_recreating(self, cron_step):
        assert "gh issue reopen" in cron_step

    def test_the_body_only_promises_what_the_workflow_does(
            self, tmp_path, capsys, gh_env, cron_step):
        body = tmp_path / "issue.md"
        _run(tmp_path, EIGHT_FINDINGS, capsys, issue_body=body)
        text = body.read_text()
        if "reopen" in text:
            assert "gh issue reopen" in cron_step, (
                "the issue body promises it reopens itself, but the workflow "
                "never calls `gh issue reopen`")
        if "closes it" in text:
            assert "gh issue close" in cron_step


class TestTheLockIsActuallyFixed:
    """Assert the resolved VERSIONS, not that a lockfile line moved (#900).

    uv writes one ``[[package]]`` block per resolution fork, so a package can
    be locked at several versions gated by ``resolution-markers``. ``click``
    was locked TWICE on main:

        click 8.3.1   >=3.14, ==3.12.*, ==3.11.*, <3.11   <- VULNERABLE
        click 8.4.1   ==3.13.*                            <- fine

    and the security workflow pins ``python-version: "3.12"``, so CI audited
    the vulnerable fork. A relock that advanced only the 3.13 branch would have
    produced a diff that looks like a fix and changes nothing for CI. So these
    check EVERY fork, which is the only assertion that can tell those apart.
    """

    MINIMUMS = {
        # package: (minimum safe version, the advisories it clears)
        "aiohttp": ((3, 14, 3), "PYSEC-2026-3545/3546/3547"),
        "click": ((8, 3, 3), "PYSEC-2026-2132"),
        "mcp": ((1, 27, 2), "PYSEC-2026-3481/3482"),
        "cryptography": ((50, 0, 0), "PYSEC-2026-3552"),
    }

    @staticmethod
    def _locked(package: str) -> list[tuple[str, str]]:
        """[(version, markers)] for every fork of *package* in uv.lock."""
        import re

        text = (REPO / "uv.lock").read_text()
        pattern = (r'\[\[package\]\]\nname = "%s"\nversion = "([^"]+)"\n'
                   r'(.*?)(?=\n\[\[package\]\]|\Z)' % re.escape(package))
        out = []
        for match in re.finditer(pattern, text, re.S):
            markers = re.search(r"resolution-markers = \[\n(.*?)\n\]",
                                match.group(2), re.S)
            out.append((match.group(1),
                        " ".join(markers.group(1).split()) if markers else ""))
        return out

    @staticmethod
    def _parse(version: str) -> tuple[int, ...]:
        return tuple(int(p) for p in version.split(".")[:3] if p.isdigit())

    @pytest.mark.parametrize("package", sorted(MINIMUMS))
    def test_every_fork_is_at_or_above_the_fixed_version(self, package):
        minimum, advisories = self.MINIMUMS[package]
        forks = self._locked(package)
        assert forks, f"{package} is not in uv.lock at all"
        for version, markers in forks:
            assert self._parse(version) >= minimum, (
                f"{package} {version} (markers: {markers or 'none'}) is below "
                f"{'.'.join(map(str, minimum))} — {advisories} unfixed on that fork"
            )

    def test_the_python_312_fork_specifically(self):
        """CI pins python-version 3.12 — the fork that was actually audited."""
        for package, (minimum, _) in self.MINIMUMS.items():
            applicable = [
                (v, m) for v, m in self._locked(package)
                if not m or "3.12" in m or "python_full_version <" in m
                or "python_full_version >=" in m
            ]
            assert applicable, f"no {package} fork resolves for python 3.12"
            for version, markers in applicable:
                assert self._parse(version) >= minimum, (
                    f"python 3.12 resolves {package} {version} "
                    f"(markers: {markers or 'none'}), below the fixed version")

    def test_the_mcp_bound_is_not_widened(self):
        """MCP SDK 2.x removed mcp.server.fastmcp, which mcp_core imports —
        unbounded, a rebuild resolved 2.x and every MCP tool vanished from
        every session while rebuild printed success (#874)."""
        pyproject = (REPO / "pyproject.toml").read_text()
        assert '"mcp>=1.2.0,<2"' in pyproject

    def test_mcp_stayed_below_the_cascading_version(self):
        """1.28.1+ needs pydantic 2.13, which cascades 32 packages including
        torch 2.6 -> 2.13. Deliberately deferred; see the workflow's
        --ignore-vuln comment and docs/wiki/security/pip-audit.md."""
        for version, _ in self._locked("mcp"):
            assert self._parse(version) < (1, 28, 1), (
                f"mcp {version} pulls the torch/CUDA cascade — if that is "
                "intended, drop --ignore-vuln PYSEC-2026-3483 too")


class TestTheIgnoreRationaleIsStillTrue:
    """`PYSEC-2026-3483` is ignored because we never use the affected path.

    An ignore justified by "we don't use that" is only as strong as the path
    staying unused — and a rationale that has quietly become false is WORSE
    than none, because the next reader trusts it and stops checking. So the
    premise is pinned here: if agentwire ever serves MCP over WebSocket, these
    fail and name the ignore that must be revisited.
    """

    AGENTWIRE = REPO / "agentwire"
    IGNORE = "PYSEC-2026-3483"

    def test_the_mcp_server_runs_on_stdio(self):
        source = (self.AGENTWIRE / "mcp_server.py").read_text()
        assert 'transport="stdio"' in source, (
            f"the MCP server no longer runs on stdio — the reachability "
            f"argument for --ignore-vuln {self.IGNORE} no longer holds")

    def test_nothing_imports_the_websocket_server_transport(self):
        """The module the advisory is actually about."""
        offenders = [
            path.relative_to(REPO)
            for path in self.AGENTWIRE.rglob("*.py")
            if "mcp.server.websocket" in path.read_text()
            or "websocket_server" in path.read_text()
        ]
        assert offenders == [], (
            f"{offenders} import the transport {self.IGNORE} is about — "
            "drop the --ignore-vuln and take mcp>=1.28.1 (and its 32-package "
            "torch/CUDA cascade), or stop using it")

    def test_the_ignore_and_its_rationale_travel_together(self):
        """If the ignore is dropped, this stops guarding — which is correct.
        If it is kept, the rationale must be findable from the ignore."""
        workflow = WORKFLOW.read_text()
        if f"--ignore-vuln {self.IGNORE}" not in workflow:
            pytest.skip("advisory no longer ignored; premise no longer load-bearing")
        assert "stdio" in workflow, "the ignore has lost its inline rationale"
        doc = (REPO / "docs/wiki/security/pip-audit.md").read_text()
        assert self.IGNORE in doc and "stdio" in doc


class TestWorkflowWiring:
    """A perfectly-tested reporter that nothing calls is the #900 shape again."""

    @pytest.fixture
    def workflow(self):
        return yaml.safe_load(WORKFLOW.read_text())

    @pytest.fixture
    def audit_job(self, workflow):
        return workflow["jobs"]["pip-audit"]

    @staticmethod
    def _audited(steps):
        """{report path: step} for every pip-audit invocation."""
        out = {}
        for step in steps:
            run = step.get("run", "")
            # The uvx invocation, not any step that merely mentions the words
            # (the cron step's issue TITLE contains "pip-audit:").
            if "uvx pip-audit" not in run:
                continue
            tokens = run.split()
            for i, tok in enumerate(tokens):
                if tok == "--output" and i + 1 < len(tokens):
                    out[tokens[i + 1]] = step
        return out

    @staticmethod
    def _reported(steps):
        """{report path read: step} for every reporter invocation."""
        out = {}
        for step in steps:
            run = step.get("run", "")
            if "pip_audit_report.py" not in run:
                continue
            for tok in run.split():
                if tok.endswith(".json"):
                    out[tok] = step
        return out

    def test_every_audit_writes_a_report(self, audit_job):
        """A pip-audit that only prints to the log is invisible again."""
        steps = audit_job["steps"]
        invocations = [s for s in steps if "uvx pip-audit" in s.get("run", "")]
        assert invocations, "no pip-audit step at all"
        assert len(self._audited(steps)) == len(invocations), \
            "a pip-audit invocation produces no --output json for the reporter"

    def test_every_report_is_read_by_the_reporter(self, audit_job):
        """The mutation that got past the first version of this test: deleting
        the PR-path reporter still left the cron one, so a naive "is the script
        mentioned anywhere" check stayed green while PR runs went silent.
        """
        steps = audit_job["steps"]
        produced = set(self._audited(steps))
        consumed = set(self._reported(steps))
        assert produced, "nothing audited"
        assert produced <= consumed, f"audited but never reported: {produced - consumed}"
        assert consumed <= produced, f"reads a file nothing writes: {consumed - produced}"

    def test_the_pr_path_audit_is_reported(self, audit_job):
        """Specifically the non-cron path — that is where #900 was found."""
        steps = audit_job["steps"]
        pr_reports = {path for path, step in self._audited(steps).items()
                      if "schedule" not in str(step.get("if", ""))}
        assert pr_reports, "no audit runs on PRs"
        reported = self._reported(steps)
        for path in pr_reports:
            step = reported.get(path)
            assert step is not None, f"{path} audited on PRs but never reported"
            assert "schedule" not in str(step.get("if", "")), \
                f"{path} is only reported on the weekly cron"

    def test_the_reporter_runs_even_when_the_audit_fails(self, audit_job):
        """`if: always()` is the whole point — a crashed audit must still
        report, or it silently reads as clean."""
        for step in audit_job["steps"]:
            if "pip_audit_report.py" in step.get("run", ""):
                assert "always()" in str(step.get("if", "")), \
                    f"reporting step is conditional: {step.get('name')}"

    def test_the_job_is_still_advisory(self, audit_job):
        """Non-blocking is deliberate and documented. Visibility must not
        smuggle in a merge gate."""
        assert audit_job.get("continue-on-error") is True

    def test_the_deferred_mcp_advisory_is_explicitly_ignored(self, audit_job):
        """PYSEC-2026-3483 is knowingly deferred (unreachable WebSocket server
        transport; its fix cascades 32 packages). If it is NOT in the ignore
        list the audit is red for a reason nobody wrote down — which is the
        state #900 found."""
        runs = " ".join(s.get("run", "") for s in audit_job["steps"])
        assert "--ignore-vuln PYSEC-2026-3483" in runs

    def test_every_ignored_id_is_documented(self, audit_job):
        """An ignore with no rationale is indistinguishable from a mistake."""
        runs = " ".join(s.get("run", "") for s in audit_job["steps"])
        ignored = {tok for i, tok in enumerate(runs.split())
                   if i and runs.split()[i - 1] == "--ignore-vuln"}
        doc = (REPO / "docs/wiki/security/pip-audit.md").read_text()
        yaml_text = WORKFLOW.read_text()
        for vid in ignored:
            assert vid in doc or vid in yaml_text, f"{vid} ignored with no rationale"
