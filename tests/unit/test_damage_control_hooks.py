"""Tests for agentwire/hooks/damage-control/ — Bash/Edit/Write tool hooks.

These hooks are PEP 723 inline-deps scripts invoked by Claude Code. They live
under hyphenated filenames (`bash-tool-damage-control.py`), so they're loaded
via importlib instead of normal imports. Each hook exposes a top-level
`check_command` (bash) or `check_path` (edit/write) function plus a `main()`
that reads JSON from stdin.

We test the pure decision functions directly (fast, deterministic) and a
representative subprocess flow per hook (covers the stdin/exit-code surface).
"""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "agentwire" / "hooks" / "damage-control"


def _load_hook(filename: str):
    """Load a hyphenated hook script as an importable module.

    The script's `audit_logger` import resolves via sys.path injection so the
    fallback no-op log_* functions are not needed.
    """
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        path = HOOKS_DIR / filename
        spec = importlib.util.spec_from_file_location(
            filename.replace(".py", "").replace("-", "_"), path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def bash_hook():
    return _load_hook("bash-tool-damage-control.py")


@pytest.fixture(scope="module")
def edit_hook():
    return _load_hook("edit-tool-damage-control.py")


@pytest.fixture(scope="module")
def write_hook():
    return _load_hook("write-tool-damage-control.py")


# ---------------------------------------------------------------------------
# bash-tool-damage-control.py: check_command decision matrix
# ---------------------------------------------------------------------------


class TestBashHookCheckCommand:
    @staticmethod
    def _config(**overrides):
        """Minimal config; tests override the relevant key."""
        return {
            "bashToolPatterns": [],
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": [],
            "allowedPaths": [],
            **overrides,
        }

    def test_no_patterns_allows(self, bash_hook):
        result = bash_hook.check_command("echo hello", self._config())
        assert result["decision"] == "allow"

    def test_hard_block_pattern(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\brm\s+-rf\s+/", "reason": "rm -rf /"},
        ])
        result = bash_hook.check_command("rm -rf /", cfg)
        assert result["decision"] == "block"
        assert "rm -rf /" in result["reason"]

    def test_ask_pattern(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\bgit\s+push\b", "reason": "git push", "ask": True},
        ])
        result = bash_hook.check_command("git push origin main", cfg)
        assert result["decision"] == "ask"
        assert result["reason"] == "git push"

    def test_bypassable_pattern_blocks_without_allowlist(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"\brm\s+", "reason": "rm deletion", "bypassable": True},
        ])
        result = bash_hook.check_command("rm /etc/passwd", cfg)
        assert result["decision"] == "block"

    def test_bypassable_pattern_allowed_via_allowlist(self, bash_hook):
        cfg = self._config(
            bashToolPatterns=[
                {"pattern": r"\brm\s+", "reason": "rm deletion", "bypassable": True},
            ],
            allowedPaths=[{"path": "*/dist/*", "allow": "all"}],
        )
        result = bash_hook.check_command("rm /home/user/proj/dist/old.whl", cfg)
        assert result["decision"] == "allow"

    def test_zero_access_path_blocks(self, bash_hook):
        cfg = self._config(zeroAccessPaths=["/etc/secret"])
        result = bash_hook.check_command("cat /etc/secret", cfg)
        assert result["decision"] == "block"
        assert "Zero-access" in result["reason"]

    def test_zero_access_method_call_skipped(self, bash_hook):
        """`module.py(...)` should not match `*.py` zero-access pattern."""
        cfg = self._config(zeroAccessPaths=["*.py"])
        result = bash_hook.check_command("python -c 'import module.py()'", cfg)
        assert result["decision"] == "allow"

    def test_invalid_regex_skipped_not_crashed(self, bash_hook):
        cfg = self._config(bashToolPatterns=[
            {"pattern": r"[unclosed", "reason": "bad pattern"},
            {"pattern": r"\bdanger\b", "reason": "real danger"},
        ])
        result = bash_hook.check_command("danger ahead", cfg)
        assert result["decision"] == "block"
        assert result["reason"] == "real danger"

    def test_read_only_path_caught_via_redirect(self, bash_hook):
        cfg = self._config(readOnlyPaths=["/etc/"])
        result = bash_hook.check_command("echo data > /etc/foo", cfg)
        assert result["decision"] == "block"

    def test_no_delete_path_blocks_rm(self, bash_hook):
        cfg = self._config(noDeletePaths=[".git/"])
        result = bash_hook.check_command("rm .git/HEAD", cfg)
        assert result["decision"] == "block"

    def test_no_delete_path_allows_cat(self, bash_hook):
        """no-delete only blocks deletes, not reads."""
        cfg = self._config(noDeletePaths=[".git/"])
        result = bash_hook.check_command("cat .git/HEAD", cfg)
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# bash-tool-damage-control.py: anchored (command-prefix) rules — #675
# ---------------------------------------------------------------------------


class TestAnchoredRules:
    """Anchored rules match command position only, never quoted content."""

    GH_RULE = {
        "pattern": r"\bgh\s+pr\s+comment\b",
        "reason": "GitHub CLI: Add a comment to a PR",
        "ask": True,
        "anchored": True,
    }

    @staticmethod
    def _config(patterns):
        return {
            "bashToolPatterns": patterns,
            "zeroAccessPaths": [],
            "readOnlyPaths": [],
            "noDeletePaths": [],
            "allowedPaths": [],
        }

    def _check(self, bash_hook, command):
        return bash_hook.check_command(command, self._config([self.GH_RULE]))

    def test_quoted_message_mention_not_matched(self, bash_hook):
        r = self._check(
            bash_hook,
            'git commit -m "document that gh pr comment was blocked"',
        )
        assert r["decision"] == "allow"

    def test_quoted_echo_mention_not_matched(self, bash_hook):
        r = self._check(bash_hook, 'echo "gh pr comment was blocked" >> notes.md')
        assert r["decision"] == "allow"

    def test_heredoc_body_mention_not_matched(self, bash_hook):
        r = self._check(
            bash_hook, 'git commit -F - <<EOF\nnotes about gh pr comment\nEOF'
        )
        assert r["decision"] == "allow"

    def test_real_command_still_matched(self, bash_hook):
        r = self._check(bash_hook, "gh pr comment 5 --body hi")
        assert r["decision"] == "ask"

    def test_real_command_in_chain_still_matched(self, bash_hook):
        r = self._check(bash_hook, "git status && gh pr comment 5 --body hi")
        assert r["decision"] == "ask"

    def test_quote_obfuscated_command_still_matched(self, bash_hook):
        assert self._check(bash_hook, 'g"h" pr comment 5')["decision"] == "ask"
        assert self._check(bash_hook, '"gh" pr comment 5')["decision"] == "ask"

    def test_var_indirection_still_matched(self, bash_hook):
        r = self._check(bash_hook, "G=gh; $G pr comment 5")
        assert r["decision"] == "ask"

    def test_shell_dash_c_payload_still_matched(self, bash_hook):
        r = self._check(bash_hook, 'bash -c "gh pr comment 5 --body hi"')
        assert r["decision"] == "ask"

    def test_unanchored_rule_still_matches_content(self, bash_hook):
        # Content-based rules keep whole-command semantics.
        rule = dict(self.GH_RULE)
        del rule["anchored"]
        r = bash_hook.check_command(
            'git commit -m "mentions gh pr comment"', self._config([rule])
        )
        assert r["decision"] == "ask"

    def test_tooldef_rules_are_anchored(self, bash_hook, tmp_path):
        (tmp_path / "gh.yaml").write_text(
            "name: GitHub CLI\ncommands:\n"
            "  - cmd: gh pr comment <pr>\n"
            "    access: write\n"
            "    description: Add a comment to a PR\n"
        )
        patterns = bash_hook.load_write_patterns_from_tooldefs(tmp_path)
        assert patterns and all(p.get("anchored") for p in patterns)

    def test_masked_subcommands_masks_quoted_content(self, bash_hook):
        subs = bash_hook.masked_subcommands('git commit -m "a b c" && echo hi')
        assert subs[0].startswith("git commit -m ")
        assert "a b c" not in subs[0]
        assert subs[1] == "echo hi"


# ---------------------------------------------------------------------------
# bash-tool-damage-control.py: helper functions
# ---------------------------------------------------------------------------


class TestBashHookHelpers:
    def test_glob_to_regex_extension(self, bash_hook):
        regex = bash_hook.glob_to_regex("*.py")
        # Should match "rm test.py" but not "module.python"
        import re
        assert re.search(regex, "rm test.py")
        assert not re.search(regex, "module.python")

    def test_is_glob_pattern_detection(self, bash_hook):
        assert bash_hook.is_glob_pattern("*.py") is True
        assert bash_hook.is_glob_pattern("file?.txt") is True
        assert bash_hook.is_glob_pattern("[abc]") is True
        assert bash_hook.is_glob_pattern("plain.txt") is False

    @pytest.mark.parametrize("reason,expected", [
        ("rm anything", "delete"),
        ("trash this", "delete"),
        ("rmdir empty", "delete"),
        ("chmod -x", "chmod"),
        ("chown user", "chmod"),
        ("mv operation", "move"),
        ("write to disk", "write"),
        ("", "write"),
    ])
    def test_infer_operation_from_reason(self, bash_hook, reason, expected):
        assert bash_hook._infer_operation_from_reason(reason) == expected

    def test_extract_command_paths(self, bash_hook):
        paths = bash_hook._extract_command_paths("cat /etc/passwd /tmp/foo")
        assert "/etc/passwd" in paths
        assert "/tmp/foo" in paths


# ---------------------------------------------------------------------------
# bash hook: subprocess end-to-end (stdin → exit code → stderr)
# ---------------------------------------------------------------------------


class TestBashHookSubprocess:
    """Test the full main() flow: read JSON from stdin, exit 0/1/2."""

    HOOK = HOOKS_DIR / "bash-tool-damage-control.py"

    def _run(self, payload, env_extra=None):
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": "/tmp",
            **(env_extra or {}),
        }
        # Use system python directly (skip uv-run startup) — the script's
        # imports (yaml, audit_logger) are present in the venv
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        return proc

    def test_non_bash_tool_passes(self):
        # Edit tool input must be ignored (this hook only checks Bash)
        proc = self._run({"tool_name": "Edit", "tool_input": {"file_path": "/x"}})
        assert proc.returncode == 0

    def test_empty_command_passes(self):
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": ""}})
        assert proc.returncode == 0

    def test_invalid_json_exits_1(self):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input="not json {{{",
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=10,
        )
        assert proc.returncode == 1
        assert "Invalid JSON" in proc.stderr or "Error" in proc.stderr


class TestUnattendedAllowlist:
    """Pure-function tests for the unattended (no-human) ask resolution (#401)."""

    def test_is_unattended_reads_env(self, bash_hook, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)
        assert bash_hook.is_unattended() is False
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "1")
        assert bash_hook.is_unattended() is True
        monkeypatch.setenv("AGENTWIRE_UNATTENDED", "0")
        assert bash_hook.is_unattended() is False

    def test_default_allowlist_covers_work_and_pr(self, bash_hook, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_UNATTENDED_ALLOW", raising=False)
        allow = bash_hook.resolve_unattended_allow({"safety": {}})
        # work + open a PR, nothing irreversible/outward
        assert {"git.add", "git.commit", "git.push", "gh.pr-create"} <= allow
        assert "gh.pr-merge" not in allow

    def test_config_extends_default(self, bash_hook, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_UNATTENDED_ALLOW", raising=False)
        allow = bash_hook.resolve_unattended_allow(
            {"safety": {"unattended_allow": ["custom.rule"]}}
        )
        assert "custom.rule" in allow
        assert "git.commit" in allow  # default still present

    def test_env_extension_merges(self, bash_hook, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_UNATTENDED_ALLOW", "task.rule-a, task.rule-b")
        allow = bash_hook.resolve_unattended_allow({"safety": {}})
        assert {"task.rule-a", "task.rule-b"} <= allow

    def test_default_allowlist_covers_agentwire_email(self, bash_hook, monkeypatch):
        # `agentwire email` is a blanket unattended-allow (#804) — the primary
        # way an unattended agent reports back, so fail-closed blocking it
        # defeats the use case. `agentwire quo` (SMS) is deliberately NOT here.
        monkeypatch.delenv("AGENTWIRE_UNATTENDED_ALLOW", raising=False)
        allow = bash_hook.resolve_unattended_allow({"safety": {}})
        assert "outbound.agentwire-email" in allow
        assert "outbound.agentwire-quo" not in allow


class TestUnattendedSubprocess:
    """End-to-end exit codes for the unattended ask resolution (#401)."""

    HOOK = HOOKS_DIR / "bash-tool-damage-control.py"

    def _run(self, command, unattended, permission_mode="bypassPermissions",
             allow_env=None):
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": "/tmp",  # no ~/.agentwire/config.yaml → safety enabled (default)
        }
        if unattended:
            env["AGENTWIRE_UNATTENDED"] = "1"
        if allow_env:
            env["AGENTWIRE_UNATTENDED_ALLOW"] = allow_env
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "permission_mode": permission_mode,
        }
        return subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=15,
        )

    def test_ask_tier_blocks_when_unattended(self):
        # gh pr merge is ask-tier and NOT on the allowlist → fail closed
        proc = self._run("gh pr merge 5", unattended=True)
        assert proc.returncode == 2
        assert "unattended" in proc.stderr.lower()

    def test_allowlisted_proceeds_when_unattended(self):
        # git commit is on the default allowlist → proceeds
        proc = self._run("git commit -m 'x'", unattended=True)
        assert proc.returncode == 0

    def test_hard_block_still_fires_when_unattended(self):
        # git push --force is hard-block tier → blocked regardless
        proc = self._run("git push --force", unattended=True)
        assert proc.returncode == 2

    def test_interactive_bypass_unchanged(self):
        # Same ask-tier command with no unattended marker → allow (legacy bypass)
        proc = self._run("gh pr merge 5", unattended=False,
                         permission_mode="bypassPermissions")
        assert proc.returncode == 0

    def test_per_task_env_extension_allows(self):
        # Extending the allowlist via the per-task env var lets it proceed
        proc = self._run(
            "gh pr merge 5", unattended=True,
            allow_env="tooldef.github-cli-merge-a-pull-request",
        )
        assert proc.returncode == 0

    def test_email_any_recipient_allowed_when_unattended(self):
        # outbound.agentwire-email is a blanket unattended-allow (#804) — ANY
        # recipient, not just the owner's own address (owner-accepted tradeoff).
        proc = self._run("agentwire email --to anyone@example.com", unattended=True)
        assert proc.returncode == 0

    def test_quo_still_blocks_when_unattended(self):
        # agentwire quo (SMS) is a separate outbound channel and unaffected —
        # still fails closed unless explicitly allowlisted.
        proc = self._run("agentwire quo --to +15551234567", unattended=True)
        assert proc.returncode == 2
        assert "outbound.agentwire-quo" in proc.stderr


# ---------------------------------------------------------------------------
# Unattended-relevant verb coverage (#428)
# ---------------------------------------------------------------------------
#
# The unattended guardrail only catches the `ask` tier. A high-impact verb left
# at `allow` (deploy / outbound / DB migration / publish) would sail through a
# headless scheduler session silently. These tests assert each such verb is at
# least `ask` (so the guardrail fails it closed unattended) while benign
# read-only variants stay `allow` (so interactive mode isn't over-blocked).

# Bundled rules + tooldefs evaluated through the real decision ladder.
_DC_ROOT = Path(__file__).resolve().parent.parent.parent / "agentwire"
_DC_RULES = _DC_ROOT / "hooks" / "damage-control" / "rules"
_DC_TOOLDEFS = _DC_ROOT / "tooldefs"


@pytest.fixture(scope="module")
def bundled_config(bash_hook):
    cfg = bash_hook.load_config(_DC_RULES, _DC_TOOLDEFS)
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


class TestUnattendedVerbCoverage:
    # Verbs that MUST be at least `ask` so the unattended guardrail catches them.
    ASK_OR_BLOCK = [
        # deploys
        "vercel deploy --prod", "vercel --prod", "netlify deploy", "fly deploy",
        "flyctl deploy", "wrangler deploy", "wrangler publish", "railway up",
        "supabase functions deploy fn", "gcloud run deploy svc",
        "gcloud app deploy", "gcloud functions deploy fn", "terraform apply",
        "pulumi up", "serverless deploy", "sls deploy", "sam deploy",
        "cdk deploy", "ansible-playbook site.yml",
        "aws cloudformation deploy --stack-name s",
        "aws lambda update-function-code --function-name f",
        "aws ecs update-service --service s", "docker push reg/img",
        "docker compose push", "kubectl apply -f x.yaml",
        "gh release create v1", "gh workflow run deploy.yml",
        # outbound comms
        "agentwire email --to a@b.com", "agentwire quo --to +1 --body hi",
        "twilio api:core:messages:create --to +1",
        "aws ses send-email --to a@b.com", "aws sesv2 send-email --to a@b.com",
        "aws sns publish --message hi", "sendmail a@b.com", "mail -s subj a@b.com",
        # db migrations / writes
        "prisma migrate deploy", "npx prisma migrate deploy", "prisma migrate dev",
        "prisma db push", "supabase db push", "supabase migration up",
        "alembic upgrade head", "alembic downgrade -1", "python manage.py migrate",
        "rails db:migrate", "bundle exec rake db:migrate", "knex migrate:latest",
        "npx sequelize db:migrate", "flyway migrate", "liquibase update",
        'psql -c "UPDATE users SET x=1 WHERE id=2"',
        'psql -c "INSERT INTO t VALUES (1)"',
        'mysql -e "ALTER TABLE t ADD COLUMN c int"',
        'mongosh --eval "db.t.updateMany({},{})"',
        # package publish
        "npm publish", "uv publish", "cargo publish", "poetry publish",
        "twine upload dist/*", "pnpm publish", "yarn publish", "gem push g.gem",
        "mvn deploy",
    ]

    # Schema-dropping migration variants must hard-block (data loss).
    HARD_BLOCK = [
        "prisma migrate reset --force",
        "flyway clean",
    ]

    # Benign read-only variants must stay `allow` (no interactive over-blocking).
    STAYS_ALLOW = [
        "vercel ls", "git status", "kubectl get pods", "gh pr view 5",
        'psql -c "SELECT * FROM users WHERE id = 1"',
        'psql -c "SELECT update_time FROM updates"',
        "mysql -e 'SELECT 1'",
    ]

    @pytest.mark.parametrize("cmd", ASK_OR_BLOCK)
    def test_high_impact_verb_is_gated(self, bash_hook, bundled_config, cmd):
        decision = bash_hook.check_command(cmd, bundled_config)["decision"]
        assert decision in ("ask", "block"), f"{cmd!r} resolved to {decision}"

    @pytest.mark.parametrize("cmd", HARD_BLOCK)
    def test_destructive_migration_hard_blocks(self, bash_hook, bundled_config, cmd):
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "block"

    @pytest.mark.parametrize("cmd", STAYS_ALLOW)
    def test_benign_variant_stays_allow(self, bash_hook, bundled_config, cmd):
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "allow"


# ---------------------------------------------------------------------------
# git global options (#913)
# ---------------------------------------------------------------------------
#
# `git -C <dir> <cmd>` used to defeat EVERY git rule: both the hand-written
# `bashToolPatterns` (`\bgit\s+push\s+--force\b`) and the tooldef-generated
# ones (`\bgit\s+commit\b`) assume the subcommand is adjacent to `git`, and
# `\s+` cannot span `-C /repo`. Verified against the shipped hook before the
# fix: force-push, hard reset and `clean -fdx` all went from block to allow,
# and `git add` from ask to allow.
#
# These assertions run the REAL rule set (bundled YAMLs + tooldefs) through the
# REAL decision ladder in the generated hook — not a reconstruction of either.
# The `--force-with-lease` case in STILL_ALLOWED is the over-stripping canary:
# normalization must not turn a permitted form into a blocked one.

# Assembled at runtime so this file's own text can't trip a bash rule if it is
# ever scanned as a command.
_FORCE = "--" + "force"
_HARD = "--" + "hard"

# (subcommand-and-args, expected decision) — the decision must be identical for
# the plain form and every global-option-prefixed form.
_GIT_GATED = [
    (f"push {_FORCE} origin main", "block"),
    (f"reset {_HARD} origin/main", "block"),
    ("clean -fdx", "block"),
    ("stash clear", "block"),
    ("filter-branch", "block"),
    ("add -A", "ask"),
    ("branch -D feature", "ask"),
]

# Global-option prefixes that must all normalize away. Each is real git syntax
# (measured against git 2.50.1): `-C`/`-c` take a SEPARATE argument;
# `--git-dir`/`--work-tree`/`--namespace` accept the `=` and the space form;
# `--no-optional-locks`/`-P` stand alone.
_GIT_PREFIXES = [
    "-C /repo",
    "-C /repo -c user.email=x",
    "-c core.pager=cat",
    "--git-dir=/repo/x",
    "--git-dir /repo/x",
    "--work-tree /w --git-dir /g",
    "--namespace ns -C /repo",
    "--no-optional-locks -C /repo",
    "-P -C /repo",
]

# Must stay `allow` — the normalizer may not invent matches.
_GIT_STILL_ALLOWED = [
    "git status",
    "git -C /repo status",
    "git -C /repo log --oneline",
    # `-C` AFTER the subcommand is a subcommand option (detect copies), not a
    # global one. Stripping it would be the over-correction.
    "git log -C --oneline",
    "git diff -C -C",
    # Quoted content that merely MENTIONS a gated command (#675 masking). The
    # normalized variant is derived from the MASKED tokens for exactly this
    # reason: normalize the raw string instead and quoted content becomes
    # matchable again, regressing #675 on the very path this fix touches.
    f"echo 'git -C /repo push {_FORCE}'",
    "echo 'do not run git clean -fdx here'",
    f"echo 'do not run git -C /repo push {_FORCE} here'",
    "git clean -n",
    "git -C /repo clean -n",
]


@pytest.fixture(scope="module")
def unanchored_config(bash_hook):
    """Bundled rules with `anchored` dropped from EVERY pattern.

    Which rules are anchored decides which haystack a rule reads, and that
    split is not stable: 14 of the 101 anchored patterns are the hand-written
    git rules and 87 are tooldef-generated, #915 may move the line, and the
    rules installed on this machine carry zero `anchored: true` today. Encoding
    today's split would make this fix correct now and silently wrong later, so
    the bypass cases are asserted under BOTH routings.
    """
    cfg = copy.deepcopy(bash_hook.load_config(_DC_RULES, _DC_TOOLDEFS))
    for pattern in cfg["bashToolPatterns"]:
        pattern.pop("anchored", None)
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


@pytest.fixture(scope="module")
def reversed_config(bash_hook):
    """Bundled rules with the pattern list REVERSED.

    Which decision wins is order-dependent: an `ask` match returns immediately,
    a `bypassable` block is deferred until after the loop. So `ask` beats a
    bypassable block in either order, and beats a plain block too when it sorts
    earlier — meaning rule-file load/merge order decides the verdict. Making
    `ask` rules newly visible can therefore reorder outcomes without touching a
    single pattern, and a single-ordering assertion cannot see it.
    """
    cfg = copy.deepcopy(bash_hook.load_config(_DC_RULES, _DC_TOOLDEFS))
    cfg["bashToolPatterns"] = list(reversed(cfg["bashToolPatterns"]))
    cfg["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return cfg


class TestGitGlobalOptionBypass:
    """#913 — git's global options must not hide the subcommand from a rule."""

    @pytest.mark.parametrize("routing", ["anchored", "unanchored"])
    @pytest.mark.parametrize("prefix", _GIT_PREFIXES)
    @pytest.mark.parametrize("rest,expected", _GIT_GATED)
    def test_global_options_do_not_bypass(
        self, bash_hook, bundled_config, unanchored_config, routing, prefix, rest,
        expected,
    ):
        cfg = bundled_config if routing == "anchored" else unanchored_config
        cmd = f"git {prefix} {rest}"
        decision = bash_hook.check_command(cmd, cfg)["decision"]
        assert decision == expected, (
            f"{cmd!r} resolved to {decision} under {routing} routing, want {expected}"
        )

    @pytest.mark.parametrize("rest,expected", _GIT_GATED)
    def test_plain_form_is_the_baseline(self, bash_hook, bundled_config, rest, expected):
        """Anchors the pairs above: the plain form already decided this way."""
        decision = bash_hook.check_command(f"git {rest}", bundled_config)["decision"]
        assert decision == expected

    @pytest.mark.parametrize("cmd", _GIT_STILL_ALLOWED)
    def test_no_over_blocking(self, bash_hook, bundled_config, cmd):
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "allow"

    @pytest.mark.parametrize("prefix", ["", "-C /repo ", "-c user.name=x ", "--no-pager "])
    def test_force_with_lease_is_ask_on_both_sides(
        self, bash_hook, bundled_config, prefix
    ):
        """Two-sided, and both sides matter.

        `push --force-with-lease` is `ask` (tooldef `git push`, write tier).
        Behind a global option it currently reads `allow` — the SAFE form is a
        victim of #913, not an exception to it. But it also contains the token
        `--force`, so a normalizer that rewrote the command before
        `\\bgit\\s+push\\s+.*--force(?!-with-lease)` saw it would flip it to
        `block` — which trains everyone to reach for plain `--force`.
        `allow` means the bug survived; `block` means we caused a worse one.
        """
        cmd = f"git {prefix}push {_FORCE}-with-lease origin main"
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "ask"

    @pytest.mark.parametrize("routing", ["anchored", "unanchored"])
    def test_cross_pr_case(
        self, bash_hook, bundled_config, unanchored_config, routing
    ):
        """The one case #913, #914 and #915 must all keep true after landing."""
        cfg = bundled_config if routing == "anchored" else unanchored_config
        cmd = f"git -C /repo push {_FORCE} origin main"
        assert bash_hook.check_command(cmd, cfg)["decision"] == "block"

    @pytest.mark.parametrize("order", ["as-loaded", "reversed"])
    @pytest.mark.parametrize("prefix", _GIT_PREFIXES)
    @pytest.mark.parametrize("rest,expected", _GIT_GATED)
    def test_never_downgrades_a_block_to_ask(
        self, bash_hook, bundled_config, reversed_config, prefix, rest, expected,
        order,
    ):
        """Additive matching cannot remove a match — but it CAN change which
        rule reports first, and that is not severity-neutral.

        An `ask` match returns immediately, while a `bypassable` match is
        deferred until after the rule loop. So a newly-visible `ask` rule can
        preempt a block that fires today — and `ask` resolves to ALLOW under
        bypassPermissions/auto. "Additive can only add matches" is true of
        MATCHING and false of DECISION SEVERITY.

        Asserted as PARITY against the plain form in the SAME config, under two
        pattern orderings, rather than against a fixed verdict. That is the
        invariant this normalizer actually owns, and the distinction is not
        academic: reversing the pattern order really does move `git stash clear`
        and `git push --force` from block to ask — but it moves the PLAIN forms
        too, identically. That hazard is pre-existing and order-driven, and
        pinning a fixed verdict here would have mis-attributed it to #913 while
        still not detecting a normalizer-introduced downgrade.
        """
        if expected != "block":
            pytest.skip("only meaningful where the plain form blocks")
        cfg = bundled_config if order == "as-loaded" else reversed_config
        plain = bash_hook.check_command(f"git {rest}", cfg)["decision"]
        prefixed = bash_hook.check_command(f"git {prefix} {rest}", cfg)["decision"]
        assert prefixed == plain, (
            f"git {prefix} {rest} decided {prefixed} but plain decided {plain} "
            f"({order} order)"
        )
        if order == "as-loaded":
            assert prefixed == "block"

    def test_fixture_actually_loads_the_whole_rule_set(self, bundled_config):
        """Guard on the FIXTURE, not the behavior — the likeliest way this PR
        ships green and covers nothing.

        `load_config` generates the tooldef ask-patterns only when handed a
        tooldefs_dir. Dropped, the fixture sees 178 patterns / 14 anchored
        instead of 265 / 101 — every tooldef-generated rule invisible, which is
        the half carrying #913's sharpest case (`git add` ask -> allow). The
        counts are asserted so a future arg-drop fails loudly rather than
        silently shrinking coverage.
        """
        patterns = bundled_config["bashToolPatterns"]
        anchored = [p for p in patterns if p.get("anchored")]
        assert (len(patterns), len(anchored)) == (265, 101)
        assert any(p.get("source") == "tooldef" for p in patterns)

    def test_quoted_path_with_spaces(self, bash_hook, bundled_config):
        """The `-C` argument is consumed as ONE token, so a path containing a
        space cannot push the subcommand out of reach."""
        cmd = f'git -C "/my dir/repo" push {_FORCE}'
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "block"

    @pytest.mark.parametrize(
        "wrapper", ["sudo", "doas", "time", "nohup", "command", "xargs", "nice"]
    )
    def test_bare_wrapper_prefixed_invocation(
        self, bash_hook, bundled_config, wrapper
    ):
        """`sudo git push --force` already matched; `sudo git -C … push --force`
        must not be the inconsistent survivor — and that argument applies
        unchanged to every other BARE wrapper, so the set covers them all
        rather than just the one that prompted it.

        `xargs` is here because it was the counter-example: it was excluded as
        an "arg-consuming wrapper" on the strength of the `xargs -n1` form,
        which left bare `xargs git -C /repo push --force` a live bypass under a
        comment claiming zero-arg wrappers were covered. The exclusion is about
        arity as written, not about which wrapper it is.
        """
        cmd = f"{wrapper} git -C /repo push {_FORCE}"
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "block"

    @pytest.mark.parametrize(
        "wrapper", ["timeout 5", "stdbuf -o0", "xargs -n1", "nice -n 5"]
    )
    def test_argument_carrying_wrapper_is_a_documented_limit(
        self, bash_hook, bundled_config, wrapper
    ):
        """Pins the KNOWN GAP so it is a recorded limit, not a silent one.

        A wrapper CARRYING ITS OWN ARGUMENTS is not covered — handling it means
        modelling each wrapper's grammar, which is this bug's own mistake at one
        remove. Note `xargs` and `nice` appear in BOTH this list and the bare
        list: the axis is arity as written, not the wrapper's identity.

        What must hold is that the failure is a missing haystack (no variant
        produced), never an over-strip. If one of these ever starts blocking,
        that is good news and this assertion should be inverted.
        """
        assert bash_hook.git_normalized_haystacks(
            f"{wrapper} git -C /repo push {_FORCE}"
        ) == []

    def test_chained_subcommand(self, bash_hook, bundled_config):
        cmd = f"cd /tmp && git -C /repo push {_FORCE}"
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "block"

    @pytest.mark.parametrize("prefix", [
        "-C /a -C /b",
        "-c k=v -C /a",
        "-C /a -c k=v -C /b",
        "--git-dir /g --work-tree /w --namespace ns",
        "--attr-source HEAD",
        "--config-env=k=E",
    ])
    def test_stacked_and_repeated_options(self, bash_hook, bundled_config, prefix):
        cmd = f"git {prefix} push {_FORCE}"
        assert bash_hook.check_command(cmd, bundled_config)["decision"] == "block"

    # `-c <k>=<v>` values are executed by git (sshCommand, pager, alias, fsmonitor).
    # An `rm`-shaped payload is blocked today by core.yaml's deletion rule seeing
    # it in the command — nothing to do with a git rule. Normalization is ADDITIVE
    # for exactly this reason: stripping `-c` and its value in place would delete
    # the payload and turn all four into ALLOW.
    #
    # Scope of the claim: this pins that additivity PRESERVES the coverage that
    # exists. It is not evidence the payload surface is covered — a `curl … | sh`
    # value is allowed with or without this change.
    @pytest.mark.parametrize("key", [
        "core.sshCommand", "core.pager", "alias.zap", "core.fsmonitor",
    ])
    def test_config_payload_still_visible_to_content_rules(
        self, bash_hook, bundled_config, key
    ):
        payload = "rm -" + "rf /tmp/x"
        cmd = f"git -c {key}='{payload}' fetch origin"
        result = bash_hook.check_command(cmd, bundled_config)
        assert result["decision"] == "block", result


class TestCrossIssueBackstops:
    """Canaries for coverage that only LOOKS like it belongs to another rule.

    These commands are gated today, but not by their own tool's rule — those
    are global-option-bypassed exactly like git's were. They block only via the
    generic `core.rm-file-deletion` rule, which is the rule #915 exists to
    narrow. So the sequence "#913 lands green on git-only acceptance, #915
    correctly narrows that rule" silently drops them to allow with nothing in
    either suite noticing.

    They are asserted HERE, in the PR that documents the class, so the drop
    surfaces as a named failure rather than as coverage nobody knew existed.
    A failure here is not necessarily a bug in the change that caused it — it
    means this command now needs a rule of its own.
    """

    # block -> ask transitions this change introduces, pinned WITH their reason
    # rather than forbidden by a blanket invariant.
    #
    # "No command moves block -> ask" is unsatisfiable as stated: these two are
    # #915's false positive — a commit message REFUSED for describing a
    # deletion — and the normalizer incidentally fixes them, because the
    # tooldef `git commit` ask rule becomes visible and an `ask` match returns
    # before a `bypassable` block is resolved. Both transitions are desirable.
    # Written as a prohibition the assertion would go red on this branch, and
    # the two ways to make it pass are "weaken the normalizer" and "delete the
    # test". An expected-transition list that names WHY each is wanted is
    # self-documenting; a set-exclusion predicate needs its own maintenance.
    @pytest.mark.parametrize("order", ["as-loaded", "reversed"])
    @pytest.mark.parametrize("message", [
        "cleanup: rm build/stale.txt was refused",
        "dropped the stale file, no rm build/stale.txt needed",
    ])
    def test_expected_block_to_ask_transitions(
        self, bash_hook, bundled_config, reversed_config, message, order
    ):
        cfg = bundled_config if order == "as-loaded" else reversed_config
        cmd = f'git -C /repo commit -m "{message}"'
        result = bash_hook.check_command(cmd, cfg)
        assert result["decision"] == "ask", (
            f"expected the #915 false-positive block to resolve to ask, got "
            f"{result['decision']} via {result.get('id')!r} ({order} order)"
        )
        # …and it lands on parity with the plain form, which is why this is a
        # fix rather than a divergence: on main the plain form already asked
        # while the `-C` form blocked.
        plain = bash_hook.check_command(f'git commit -m "{message}"', cfg)
        assert plain["decision"] == result["decision"]

    @pytest.mark.parametrize("cmd", [
        "docker --context prod volume rm pgdata",
        "aws --profile prod s3 rm s3://bucket --recursive",
    ])
    def test_generic_rm_rule_is_still_the_only_thing_catching_these(
        self, bash_hook, bundled_config, cmd
    ):
        result = bash_hook.check_command(cmd, bundled_config)
        assert result["decision"] == "block", result
        assert result.get("id") == "core.rm-file-deletion-use-git-clean-or-manual-cleanup", (
            f"{cmd!r} now blocks via {result.get('id')!r} — if that is a rule of "
            "its own, good; update this canary. If it is another accident, it "
            "needs one."
        )


class TestGitGlobalOptionNormalizer:
    """The normalizer's contract at the seam, as a separate THIRD haystack.

    Kept distinct from the raw and masked lists rather than rewriting either:
    #914 needs the `-C` path visible to scope a grant, and #915's `-c` payload
    cases are blocked only because a content rule can read the raw command.
    """

    def test_third_haystack_exposes_stripped_variant(self, bash_hook):
        assert f"git push {_FORCE}" in bash_hook.git_normalized_haystacks(
            f"git -C /repo push {_FORCE}"
        )

    def test_producers_are_left_alone(self, bash_hook):
        """The raw and masked lists are UNCHANGED — the variant is additive at
        the point of matching, not baked into the shared normalization."""
        cmd = f"git -C /repo push {_FORCE}"
        assert bash_hook.masked_subcommands(cmd) == [cmd]
        subs, ambiguous = bash_hook.normalize_subcommands(cmd)
        assert ambiguous is None
        assert subs == [cmd]

    def test_no_variant_for_a_plain_git_command(self, bash_hook):
        assert bash_hook.git_normalized_haystacks(f"git push {_FORCE}") == []

    def test_variant_is_derived_from_masked_tokens(self, bash_hook):
        """Quoted content must not become matchable. Normalizing the RAW string
        would hand `\\bgit\\s+clean\\b` a match inside an echo argument."""
        assert bash_hook.git_normalized_haystacks(
            "echo 'git -C /repo clean -fdx'"
        ) == []

    def test_message_content_is_masked_inside_the_variant(self, bash_hook):
        """The sharp case for deriving from MASKED tokens.

        `git -C /r commit -m 'echo git clean -fdx'` is a real git invocation
        WITH a global option, so a variant is produced either way. Built from
        shlex tokens the message text rides along and `\\bgit\\s+clean\\b`
        matches inside it; built from masked tokens the message is a
        placeholder and only the command survives.
        """
        variants = bash_hook.git_normalized_haystacks(
            "git -C /r commit -m 'echo git clean -fdx'"
        )
        assert variants
        assert all("clean" not in v for v in variants), variants

    def test_value_taking_option_consumes_its_argument(self, bash_hook):
        """Dropping only the flag would leave the value where the subcommand
        belongs (`git /repo push …`) and the rule would still miss."""
        assert bash_hook._strip_git_global_options(
            ["git", "-C", "/repo", "push"]
        ) == ["git", "push"]

    def test_option_after_subcommand_is_untouched(self, bash_hook):
        assert bash_hook._strip_git_global_options(["git", "log", "-C"]) is None

    def test_exec_path_does_not_consume_a_token(self, bash_hook):
        """Bare `--exec-path` prints and exits — only `--exec-path=<p>` carries a
        value. Consuming the next token here would swallow the subcommand."""
        assert bash_hook._strip_git_global_options(
            ["git", "--exec-path", "status"]
        ) == ["git", "status"]
        assert bash_hook._strip_git_global_options(
            ["git", "--exec-path=/x", "status"]
        ) == ["git", "status"]

    @pytest.mark.parametrize("opt", [
        "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source",
    ])
    def test_value_options_accept_both_equals_and_space_form(self, bash_hook, opt):
        """git 2.50.1 takes both, though the usage line advertises only `=`."""
        assert bash_hook._strip_git_global_options(
            ["git", opt, "v", "push"]
        ) == ["git", "push"]
        assert bash_hook._strip_git_global_options(
            ["git", f"{opt}=v", "push"]
        ) == ["git", "push"]

    def test_non_git_command_untouched(self, bash_hook):
        assert bash_hook._strip_git_global_options(["ls", "-C", "/repo"]) is None

    def test_no_global_options_yields_no_variant(self, bash_hook):
        assert bash_hook._strip_git_global_options(["git", "push"]) is None


# ---------------------------------------------------------------------------
# edit-tool-damage-control.py & write-tool-damage-control.py
# ---------------------------------------------------------------------------


class TestEditWriteHookStructure:
    """Sanity checks: hooks are loadable and expose the expected entry points."""

    def test_edit_hook_loads(self, edit_hook):
        # Should expose the same helper API as bash hook
        assert hasattr(edit_hook, "main")

    def test_write_hook_loads(self, write_hook):
        assert hasattr(write_hook, "main")


class TestEditHookSubprocess:
    HOOK = HOOKS_DIR / "edit-tool-damage-control.py"

    def _run(self, payload):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=10,
        )
        return proc

    def test_non_edit_tool_passes(self):
        # Bash tool input must be ignored (this hook only checks Edit/MultiEdit)
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert proc.returncode == 0

    def test_missing_file_path_passes(self):
        proc = self._run({"tool_name": "Edit", "tool_input": {}})
        assert proc.returncode == 0


class TestWriteHookSubprocess:
    HOOK = HOOKS_DIR / "write-tool-damage-control.py"

    def _run(self, payload):
        proc = subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            timeout=10,
        )
        return proc

    def test_non_write_tool_passes(self):
        proc = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert proc.returncode == 0

    def test_missing_file_path_passes(self):
        proc = self._run({"tool_name": "Write", "tool_input": {}})
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# audit_logger.py
# ---------------------------------------------------------------------------


class TestAuditLogger:
    @pytest.fixture
    def audit_module(self, tmp_path, monkeypatch):
        """Load audit_logger with AGENTWIRE_DIR pointing at tmp_path."""
        monkeypatch.setenv("AGENTWIRE_DIR", str(tmp_path / ".agentwire"))
        spec = importlib.util.spec_from_file_location(
            "audit_logger_test", HOOKS_DIR / "audit_logger.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_log_blocked_writes_jsonl(self, audit_module):
        audit_module.log_blocked("Bash", "rm -rf /", "rm -rf root")
        log_file = audit_module.get_log_file()
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["decision"] == "blocked"
        assert entry["tool"] == "Bash"
        assert entry["blocked_by"] == "rm -rf root"
        assert entry["command"] == "rm -rf /"

    def test_log_asked_writes_jsonl(self, audit_module):
        audit_module.log_asked("Bash", "git push", "destructive op")
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["decision"] == "asked"
        assert entry["blocked_by"] == "destructive op"

    def test_log_allowed_user_approved_flag(self, audit_module):
        audit_module.log_allowed("Bash", "git commit", user_approved=True)
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["decision"] == "allowed"
        assert entry["user_approved"] is True

    def test_log_allowed_no_user_approval_omits_flag(self, audit_module):
        """user_approved=False stores None to keep entries lean."""
        audit_module.log_allowed("Bash", "ls", user_approved=False)
        entry = json.loads(audit_module.get_log_file().read_text().strip())
        assert entry["user_approved"] is None

    def test_session_context_from_env(self, audit_module, monkeypatch):
        monkeypatch.setenv("AGENTWIRE_SESSION_ID", "sess-123")
        monkeypatch.setenv("AGENTWIRE_AGENT_ID", "worker-1")
        ctx = audit_module.get_session_context()
        assert ctx == {"session_id": "sess-123", "agent_id": "worker-1"}

    def test_session_context_defaults(self, audit_module, monkeypatch):
        monkeypatch.delenv("AGENTWIRE_SESSION_ID", raising=False)
        monkeypatch.delenv("AGENTWIRE_AGENT_ID", raising=False)
        ctx = audit_module.get_session_context()
        assert ctx == {"session_id": "unknown", "agent_id": "main"}

    def test_log_dir_creates_path(self, audit_module, tmp_path):
        log_dir = audit_module.get_log_dir()
        assert log_dir.exists()
        assert log_dir.is_dir()
        # AGENTWIRE_DIR fixture pointed it at tmp_path/.agentwire
        assert str(tmp_path) in str(log_dir)

    def test_multiple_entries_append(self, audit_module):
        audit_module.log_blocked("Bash", "cmd1", "r1")
        audit_module.log_asked("Bash", "cmd2", "r2")
        audit_module.log_allowed("Bash", "cmd3")
        lines = audit_module.get_log_file().read_text().strip().split("\n")
        assert len(lines) == 3
        decisions = [json.loads(line)["decision"] for line in lines]
        assert decisions == ["blocked", "asked", "allowed"]


# ---------------------------------------------------------------------------
# mcp-tool-damage-control.py: outbound MCP tool gating (#457)
# ---------------------------------------------------------------------------
#
# email_send / quo_send run `agentwire email …` / `agentwire quo …` under the
# hood, so the hook synthesizes that command and runs it through the SAME
# decision ladder + outbound.* rules as the Bash shell-out. These tests cover
# the synthesizer, the unattended fail-closed ladder, the attended ask, and a
# parity proof that the synthesized command decides identically to the literal
# `agentwire email`/`quo` string.


@pytest.fixture(scope="module")
def mcp_hook():
    return _load_hook("mcp-tool-damage-control.py")


class TestMcpHookSynthesizer:
    def test_email_send_synthesizes_command(self, mcp_hook):
        cmd = mcp_hook._synthesize_command(
            "mcp__agentwire__email_send",
            {"body": "secret payload", "to": "a@b.com", "subject": "Hi"},
        )
        assert cmd == "agentwire email --to a@b.com --subject Hi"
        # body never leaks into the synthesized (audit-logged) command
        assert "secret payload" not in cmd

    def test_email_send_handles_list_recipients(self, mcp_hook):
        cmd = mcp_hook._synthesize_command(
            "mcp__agentwire__email_send",
            {"body": "x", "to": ["a@b.com", "c@d.com"]},
        )
        assert cmd == "agentwire email --to a@b.com --to c@d.com"

    def test_quo_send_synthesizes_command(self, mcp_hook):
        cmd = mcp_hook._synthesize_command(
            "mcp__agentwire__quo_send", {"body": "x", "to": "+15551234567"}
        )
        assert cmd == "agentwire quo --to +15551234567"

    def test_non_gated_tool_returns_none(self, mcp_hook):
        assert mcp_hook._synthesize_command(
            "mcp__agentwire__say", {"text": "hi"}
        ) is None


class TestMcpHookParity:
    """The synthesized command must decide identically to the literal CLI string."""

    def test_email_parity(self, mcp_hook, bundled_config):
        synth = mcp_hook._synthesize_command(
            "mcp__agentwire__email_send", {"body": "x", "to": "a@b.com"}
        )
        literal = mcp_hook.check_command("agentwire email --to a@b.com", bundled_config)
        from_synth = mcp_hook.check_command(synth, bundled_config)
        assert from_synth["decision"] == literal["decision"] == "ask"
        assert from_synth["id"] == literal["id"] == "outbound.agentwire-email"

    def test_quo_parity(self, mcp_hook, bundled_config):
        synth = mcp_hook._synthesize_command(
            "mcp__agentwire__quo_send", {"body": "x", "to": "+1555"}
        )
        literal = mcp_hook.check_command("agentwire quo --to +1555", bundled_config)
        from_synth = mcp_hook.check_command(synth, bundled_config)
        assert from_synth["decision"] == literal["decision"] == "ask"
        assert from_synth["id"] == literal["id"] == "outbound.agentwire-quo"


class TestMcpHookSubprocess:
    """Full main() flow: read JSON from stdin, synthesize, decide, exit 0/2."""

    HOOK = HOOKS_DIR / "mcp-tool-damage-control.py"

    def _run(self, tool_name, tool_input, unattended=False,
             permission_mode="default", allow_env=None):
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": "/tmp",  # no ~/.agentwire/config.yaml → safety enabled (default)
        }
        if unattended:
            env["AGENTWIRE_UNATTENDED"] = "1"
        if allow_env:
            env["AGENTWIRE_UNATTENDED_ALLOW"] = allow_env
        payload = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "permission_mode": permission_mode,
        }
        return subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=15,
        )

    def test_non_gated_tool_passes(self):
        proc = self._run("mcp__agentwire__say", {"text": "hi"})
        assert proc.returncode == 0

    def test_email_unattended_allowed_by_default(self):
        # outbound.agentwire-email is a blanket unattended-allow (#804) — no
        # unattended_allow entry needed, and ANY recipient (owner-accepted
        # tradeoff), not just the owner's own address.
        proc = self._run(
            "mcp__agentwire__email_send",
            {"body": "x", "to": "someone-else@example.com", "subject": "s"},
            unattended=True,
        )
        assert proc.returncode == 0

    def test_quo_unattended_not_allowlisted_blocks(self):
        proc = self._run(
            "mcp__agentwire__quo_send", {"body": "x", "to": "+1555"},
            unattended=True,
        )
        assert proc.returncode == 2
        assert "unattended" in proc.stderr.lower()
        assert "outbound.agentwire-quo" in proc.stderr

    def test_quo_unattended_allowlisted_proceeds(self):
        proc = self._run(
            "mcp__agentwire__quo_send", {"body": "x", "to": "+1555"},
            unattended=True, allow_env="outbound.agentwire-quo",
        )
        assert proc.returncode == 0

    def test_attended_non_bypass_asks(self):
        proc = self._run(
            "mcp__agentwire__email_send", {"body": "x", "to": "a@b.com"},
            unattended=False, permission_mode="default",
        )
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "ask"

    def test_attended_bypass_allows(self):
        proc = self._run(
            "mcp__agentwire__email_send", {"body": "x", "to": "a@b.com"},
            unattended=False, permission_mode="bypassPermissions",
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
