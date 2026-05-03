"""Tests for agentwire/cli_safety.py — Glob patterns, command safety checks."""

import pytest

from agentwire.cli_safety import (
    is_glob_pattern,
    glob_to_regex,
    matches_path_in_command,
    check_command_safety,
    load_allowed_paths,
    is_command_path_allowed,
    _match_allowed_path,
    _parse_allowed_entry,
    is_path_allowed_for_op,
)


# --- is_glob_pattern ---

class TestIsGlobPattern:
    @pytest.mark.parametrize("pattern,expected", [
        ("*.py", True),
        ("file?.txt", True),
        ("[abc].txt", True),
        ("plain.txt", False),
        ("/path/to/file", False),
        ("no wildcards", False),
        ("", False),
    ])
    def test_detection(self, pattern, expected):
        assert is_glob_pattern(pattern) == expected


# --- glob_to_regex ---

class TestGlobToRegex:
    def test_star_becomes_path_safe_wildcard(self):
        # `*` in a glob must NOT cross whitespace or directory separators in
        # command-line contexts — otherwise *.py would match `arg /tmp/file.py`.
        assert "[^\\s/]*" in glob_to_regex("*.txt")

    def test_question_becomes_path_safe_single(self):
        regex = glob_to_regex("file?.txt")
        assert "[^\\s/]" in regex

    def test_dots_escaped(self):
        regex = glob_to_regex("file.txt")
        assert "\\." in regex

    def test_bracket_escaped(self):
        # Brackets are literal; escaping them keeps the regex safe inside
        # path-context wrappers.
        regex = glob_to_regex("[abc].txt")
        assert "\\[abc\\]" in regex

    def test_trailing_word_boundary(self):
        # *.py should not match foo.python — the trailing negative lookahead enforces this.
        assert glob_to_regex("*.py").endswith("(?![a-zA-Z0-9_])")


# --- matches_path_in_command ---

class TestMatchesPathInCommand:
    def test_simple_path_match(self):
        assert matches_path_in_command("/etc/passwd", "cat /etc/passwd") is True

    def test_no_match(self):
        assert matches_path_in_command("/etc/passwd", "echo hello") is False

    def test_glob_matches_file(self):
        assert matches_path_in_command("*.py", 'rm test.py') is True

    def test_glob_avoids_method_calls(self):
        # module.py() should not match *.py in a method-call context
        assert matches_path_in_command("*.py", "python -c 'import module.py()'") is False


# --- _parse_allowed_entry ---

class TestParseAllowedEntry:
    def test_dict_with_all(self):
        entry = _parse_allowed_entry({"path": "*/dist/*", "allow": "all"})
        assert entry["path"] == "*/dist/*"
        assert entry["allow"] == {"read", "write", "edit", "delete", "move", "chmod"}

    def test_dict_with_list(self):
        entry = _parse_allowed_entry({"path": "~/.env", "allow": ["read", "write"]})
        assert entry["path"] == "~/.env"
        assert entry["allow"] == {"read", "write"}

    def test_dict_with_single_string(self):
        entry = _parse_allowed_entry({"path": "/tmp/x", "allow": "read"})
        assert entry["allow"] == {"read"}

    def test_dict_defaults_to_all(self):
        entry = _parse_allowed_entry({"path": "/tmp/x"})
        assert entry["allow"] == {"read", "write", "edit", "delete", "move", "chmod"}


# --- is_path_allowed_for_op ---

class TestIsPathAllowedForOp:
    def test_all_permissions(self):
        allowed = [{"path": "*/dist/*", "allow": {"read", "write", "edit", "delete", "move", "chmod"}}]
        assert is_path_allowed_for_op("/home/user/project/dist/file.whl", allowed, "delete") is True

    def test_limited_permissions_allow(self):
        allowed = [{"path": "~/.env", "allow": {"read", "write"}}]
        assert is_path_allowed_for_op("/Users/dotdev/.env", allowed, "write") is True

    def test_limited_permissions_deny(self):
        allowed = [{"path": "~/.env", "allow": {"read", "write"}}]
        assert is_path_allowed_for_op("/Users/dotdev/.env", allowed, "delete") is False


# --- check_command_safety ---

class TestCheckCommandSafety:
    def test_allowed_with_empty_patterns(self, tmp_path, monkeypatch):
        """If RULES_DIR has no yaml files, everything is allowed."""
        import agentwire.cli_safety as mod
        monkeypatch.setattr(mod, "RULES_DIR", tmp_path / "empty-rules")

        result = check_command_safety("echo hello")
        assert result["decision"] == "allow"

    def test_result_structure(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod
        monkeypatch.setattr(mod, "RULES_DIR", tmp_path / "empty-rules")

        result = check_command_safety("ls -la")
        assert "decision" in result
        assert "reason" in result
        assert "pattern" in result
        assert "command" in result
        assert result["command"] == "ls -la"


# --- _match_allowed_path ---

class TestMatchAllowedPath:
    @pytest.mark.parametrize("path,pattern,expected", [
        # Glob match — dist anywhere in path
        ("/home/user/project/dist/file.whl", "*/dist/*", True),
        ("/home/user/.ssh/id_rsa", "*/dist/*", False),
        # Literal prefix match
        ("/tmp/test.txt", "/tmp/", True),
        ("/home/user/file.txt", "/tmp/", False),
        # Common build-artifact globs
        ("/home/user/project/__pycache__/mod.pyc", "*/__pycache__/*", True),
        ("/home/user/project/pkg.egg-info/top_level.txt", "*.egg-info/*", True),
    ])
    def test_match(self, path, pattern, expected):
        assert _match_allowed_path(path, pattern) is expected


# --- is_command_path_allowed ---

class TestIsCommandPathAllowed:
    def test_dist_path_allowed(self):
        allowed = [{"path": "*/dist/*", "allow": {"read", "write", "edit", "delete", "move", "chmod"}}]
        assert is_command_path_allowed("rm /home/user/project/dist/old.whl", allowed, "delete") is True

    def test_non_allowed_path(self):
        allowed = [{"path": "*/dist/*", "allow": {"read", "write", "edit", "delete", "move", "chmod"}}]
        assert is_command_path_allowed("rm /home/user/.ssh/id_rsa", allowed, "delete") is False

    def test_empty_allowlist(self):
        assert is_command_path_allowed("rm /tmp/test.txt", [], "delete") is False

    def test_tmp_literal(self):
        allowed = [{"path": "/tmp/", "allow": {"read", "write", "edit", "delete", "move", "chmod"}}]
        assert is_command_path_allowed("rm /tmp/test.txt", allowed, "delete") is True

    def test_all_paths_must_match(self):
        """Security: ALL paths in command must be allowed, not just any."""
        allowed = [{"path": "/tmp/", "allow": {"read", "write", "edit", "delete", "move", "chmod"}}]
        # /tmp is allowed but /etc is not — should block
        assert is_command_path_allowed("rm /tmp/safe.txt /etc/passwd", allowed, "delete") is False

    def test_operation_specific(self):
        """Path allowed for read but not delete."""
        allowed = [{"path": "/tmp/", "allow": {"read", "write"}}]
        assert is_command_path_allowed("rm /tmp/test.txt", allowed, "delete") is False
        assert is_command_path_allowed("cat /tmp/test.txt", allowed, "read") is True


# --- load_allowed_paths ---

class TestLoadAllowedPaths:
    def test_loads_structured_entries(self):
        config = {"allowedPaths": [
            {"path": "*/dist/*", "allow": "all"},
            {"path": "/tmp/*", "allow": ["read", "write"]},
        ]}
        paths = load_allowed_paths(config)
        assert len(paths) >= 2
        dist_entry = next(e for e in paths if e["path"] == "*/dist/*")
        assert "delete" in dist_entry["allow"]
        tmp_entry = next(e for e in paths if e["path"] == "/tmp/*")
        assert tmp_entry["allow"] == {"read", "write"}

    def test_plain_strings_skipped(self):
        """Plain strings are not valid entries and are skipped."""
        config = {"allowedPaths": ["*/dist/*", {"path": "/tmp/*", "allow": "all"}]}
        paths = load_allowed_paths(config)
        # Only the dict entry survives
        assert len(paths) >= 1
        assert all(isinstance(e, dict) for e in paths)
        assert any(e["path"] == "/tmp/*" for e in paths)

    def test_empty_config(self):
        result = load_allowed_paths({})
        assert isinstance(result, list)


# --- check_command_safety with allowlist ---

class TestCheckCommandSafetyAllowlist:
    def _make_rules_dir(self, tmp_path, patterns_dict):
        """Create a rules dir with a patterns.yaml and return the dir path."""
        import yaml
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir(exist_ok=True)
        with open(rules_dir / "patterns.yaml", "w") as f:
            yaml.safe_dump(patterns_dict, f)
        return rules_dir

    def test_allowlist_bypasses_readonly(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "readOnlyPaths": ["dist/"],
            "allowedPaths": [{"path": "*/dist/*", "allow": "all"}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm /home/user/project/dist/old.whl")
        assert result["decision"] == "allow"

    def test_allowlist_bypasses_nodelete(self, tmp_path, monkeypatch):
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "noDeletePaths": [".git/"],
            "allowedPaths": [],  # .git/ not allowlisted
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        # .git/ not allowlisted, should still block
        result = check_command_safety("rm .git/config")
        assert result["decision"] == "block"

    def test_hard_blocked_rm_rf_never_bypassed(self, tmp_path, monkeypatch):
        """rm -rf is hard-blocked even with all perms on target path."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "bashToolPatterns": [
                {"pattern": r"\brm\s+(-[^\s]*)*-[rRf]", "reason": "rm with flags"},
            ],
            "allowedPaths": [{"path": "/tmp/*", "allow": "all"}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm -rf /tmp/test")
        assert result["decision"] == "block"

    def test_bypassable_rm_with_delete_permission(self, tmp_path, monkeypatch):
        """Plain rm (bypassable) should be allowed if target has delete permission."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "bashToolPatterns": [
                {"pattern": r"\brm\s+[^-]", "reason": "rm file deletion", "bypassable": True},
            ],
            "allowedPaths": [{"path": "*/dist/*", "allow": "all"}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm /home/user/project/dist/old.whl")
        assert result["decision"] == "allow"

    def test_bypassable_rm_without_delete_permission(self, tmp_path, monkeypatch):
        """Plain rm (bypassable) should block if target only has read/write."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "bashToolPatterns": [
                {"pattern": r"\brm\s+[^-]", "reason": "rm file deletion", "bypassable": True},
            ],
            "allowedPaths": [{"path": "*/dist/*", "allow": ["read", "write"]}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm /home/user/project/dist/old.whl")
        assert result["decision"] == "block"

    def test_bypassable_rm_non_allowed_path(self, tmp_path, monkeypatch):
        """Plain rm (bypassable) should block if target is not in allowlist at all."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "bashToolPatterns": [
                {"pattern": r"\brm\s+[^-]", "reason": "rm file deletion", "bypassable": True},
            ],
            "allowedPaths": [{"path": "*/dist/*", "allow": "all"}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm ~/.ssh/id_rsa")
        assert result["decision"] == "block"

    def test_read_only_permission_blocks_delete(self, tmp_path, monkeypatch):
        """Path with only read permission should not bypass noDelete."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "noDeletePaths": ["README.md"],
            "allowedPaths": [{"path": "README.md", "allow": ["read"]}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("rm README.md")
        assert result["decision"] == "block"

    def test_multiple_paths_all_must_match(self, tmp_path, monkeypatch):
        """Security: ALL paths in command must be allowed for bypass."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "bashToolPatterns": [
                {"pattern": r"\brm\s+[^-]", "reason": "rm file deletion", "bypassable": True},
            ],
            "allowedPaths": [{"path": "/tmp/", "allow": "all"}],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        # One path is allowed, the other is not — should block
        result = check_command_safety("rm /tmp/safe.txt /etc/passwd")
        assert result["decision"] == "block"

    def test_empty_allowedpaths_no_change(self, tmp_path, monkeypatch):
        """With empty allowedPaths, behavior is unchanged."""
        import agentwire.cli_safety as mod
        rd = self._make_rules_dir(tmp_path, {
            "readOnlyPaths": ["dist/"],
            "allowedPaths": [],
        })
        monkeypatch.setattr(mod, "RULES_DIR", rd)

        result = check_command_safety("mv something dist/file")
        assert result["decision"] == "block"


# --- check_command_safety: pattern types & decision-order edge cases ---

class TestCheckCommandSafetyDecisionPaths:
    """Cover branches that drive evaluation order: ask, hard-block, regex error,
    zero-access bypass, no-delete bypass."""

    def _rules(self, tmp_path, monkeypatch, patterns):
        import yaml
        import agentwire.cli_safety as mod
        rd = tmp_path / "rules"
        rd.mkdir(exist_ok=True)
        (rd / "patterns.yaml").write_text(yaml.safe_dump(patterns))
        monkeypatch.setattr(mod, "RULES_DIR", rd)
        return rd

    def test_ask_pattern_returns_ask(self, tmp_path, monkeypatch):
        self._rules(tmp_path, monkeypatch, {
            "bashToolPatterns": [
                {"pattern": r"\bgit\s+push\b", "reason": "git push", "ask": True},
            ],
        })
        result = check_command_safety("git push origin main")
        assert result["decision"] == "ask"
        assert result["reason"] == "git push"

    def test_invalid_regex_skipped_not_crashed(self, tmp_path, monkeypatch):
        """A malformed pattern must not crash safety evaluation."""
        self._rules(tmp_path, monkeypatch, {
            "bashToolPatterns": [
                {"pattern": r"[unclosed", "reason": "bad pattern"},
                {"pattern": r"\bdanger\b", "reason": "real danger"},
            ],
        })
        # Bad regex skipped; real one still works.
        bad = check_command_safety("danger ahead")
        assert bad["decision"] == "block"
        assert bad["reason"] == "real danger"
        # And a benign command still allowed.
        good = check_command_safety("echo hi")
        assert good["decision"] == "allow"

    def test_non_dict_pattern_entry_skipped(self, tmp_path, monkeypatch):
        """Stray non-dict entries in bashToolPatterns must be tolerated."""
        self._rules(tmp_path, monkeypatch, {
            "bashToolPatterns": [
                "not a dict",  # bogus entry
                {"pattern": r"\bbadcmd\b", "reason": "bad"},
            ],
        })
        result = check_command_safety("badcmd run")
        assert result["decision"] == "block"

    def test_zero_access_path_blocks(self, tmp_path, monkeypatch):
        """A zero-access path (e.g. /etc/secret) blocks even read-only access."""
        self._rules(tmp_path, monkeypatch, {
            "zeroAccessPaths": ["/etc/secret"],
        })
        result = check_command_safety("cat /etc/secret")
        assert result["decision"] == "block"
        assert "Zero-access" in result["reason"]

    def test_zero_access_bypassed_by_allowlist_with_read(self, tmp_path, monkeypatch):
        """zeroAccessPaths can be bypassed if the path is allowlisted with read."""
        self._rules(tmp_path, monkeypatch, {
            "zeroAccessPaths": ["/etc/secret"],
            "allowedPaths": [{"path": "/etc/secret", "allow": ["read"]}],
        })
        result = check_command_safety("cat /etc/secret")
        assert result["decision"] == "allow"

    def test_no_delete_path_blocks_rm_only(self, tmp_path, monkeypatch):
        """noDeletePaths only flags rm — cat is fine."""
        self._rules(tmp_path, monkeypatch, {"noDeletePaths": ["README.md"]})
        # rm blocks
        assert check_command_safety("rm README.md")["decision"] == "block"
        # cat is fine
        assert check_command_safety("cat README.md")["decision"] == "allow"

    def test_no_delete_bypassed_by_allowlist_with_delete(self, tmp_path, monkeypatch):
        self._rules(tmp_path, monkeypatch, {
            "noDeletePaths": ["dist/"],
            "allowedPaths": [{"path": "*/dist/*", "allow": ["delete"]}],
        })
        result = check_command_safety("rm /tmp/proj/dist/old.whl")
        assert result["decision"] == "allow"

    def test_decision_order_ask_beats_block(self, tmp_path, monkeypatch):
        """ask wins when both ask and bypassable patterns match a command."""
        self._rules(tmp_path, monkeypatch, {
            "bashToolPatterns": [
                {"pattern": r"\brm\b", "reason": "rm anything", "ask": True},
                {"pattern": r"\brm\s+[^-]", "reason": "rm bypassable", "bypassable": True},
            ],
        })
        # Both match — ask wins because it's evaluated first.
        result = check_command_safety("rm foo.txt")
        assert result["decision"] == "ask"

    def test_no_match_allows(self, tmp_path, monkeypatch):
        self._rules(tmp_path, monkeypatch, {
            "bashToolPatterns": [
                {"pattern": r"\brm\b", "reason": "rm"},
            ],
        })
        result = check_command_safety("ls -la")
        assert result["decision"] == "allow"
        assert result["pattern"] is None


# --- _infer_operation_from_reason: dispatch by reason text ---

@pytest.mark.parametrize("reason,expected_op", [
    ("rm file deletion", "delete"),
    ("trash everything", "delete"),
    ("rmdir empty", "delete"),
    ("delete entry", "delete"),
    # Note: substring matching means "permission" -> contains "rm" -> "delete".
    # The branch order is delete-first, so chmod-only reasons must avoid "rm".
    ("chmod -x", "chmod"),
    ("chown user", "chmod"),
    ("chgrp group", "chmod"),
    ("mv operation", "move"),
    ("apply move", "move"),
    ("write to file", "write"),  # default fallback
    ("", "write"),  # empty reason → write
])
def test_infer_operation_from_reason(reason, expected_op):
    from agentwire.cli_safety import _infer_operation_from_reason
    assert _infer_operation_from_reason(reason) == expected_op


# --- _extract_command_paths: path extraction from command strings ---

class TestExtractCommandPaths:
    def _extract(self, cmd):
        from agentwire.cli_safety import _extract_command_paths
        return _extract_command_paths(cmd)

    def test_absolute_path(self):
        assert "/etc/passwd" in self._extract("cat /etc/passwd")

    def test_home_path_expanded(self):
        # ~ paths are expanded to absolute
        paths = self._extract("rm ~/.ssh/id_rsa")
        assert any(p.endswith("/.ssh/id_rsa") for p in paths)

    def test_quoted_path(self):
        paths = self._extract('cat "/etc/secret file"')
        assert "/etc/secret file" in paths

    def test_flag_excluded(self):
        # -rf is a flag, not a path
        paths = self._extract("rm -rf /tmp/x")
        assert not any(p == "-rf" or p.startswith("-") for p in paths)

    def test_relative_path(self):
        paths = self._extract("rm ./scratch.txt")
        assert any("scratch.txt" in p for p in paths)


# --- Read-only path mutation operators ---
#
# Source line ~429 catches rm/mv/sed -i/> plus cp/dd/tee/rsync/tar -c/install
# for readOnlyPath detection. Each parametrized case writes (or deletes/moves)
# to a path inside readOnlyPaths and must be blocked.

class TestReadOnlyPathMutationOperators:
    def _rules(self, tmp_path, monkeypatch, patterns):
        import yaml
        import agentwire.cli_safety as mod
        rd = tmp_path / "rules"
        rd.mkdir(exist_ok=True)
        (rd / "patterns.yaml").write_text(yaml.safe_dump(patterns))
        monkeypatch.setattr(mod, "RULES_DIR", rd)
        return rd

    @pytest.mark.parametrize("cmd", [
        # Original four (regression fence)
        "rm /readonly/file",
        "mv src /readonly/file",
        "sed -i s/x/y/g /readonly/file",
        "echo data > /readonly/file",
        # Newly closed gap (was test_uncaught_mutation_operators_currently_allowed)
        "cp src /readonly/file",
        "dd if=src of=/readonly/file",
        "tee /readonly/file",
        "rsync src /readonly/",
        "tar -cf /readonly/x.tar src",
        "tar --create -f /readonly/x.tar src",
        "install -m 0644 src /readonly/dst",
    ])
    def test_caught_mutation_operators(self, cmd, tmp_path, monkeypatch):
        self._rules(tmp_path, monkeypatch, {"readOnlyPaths": ["/readonly/"]})
        assert check_command_safety(cmd)["decision"] == "block"

    @pytest.mark.parametrize("cmd", [
        # Pure reads — must NOT trigger the read-only block
        "cat /readonly/file",
        "tar -tf /readonly/x.tar",
        "grep foo /readonly/file",
    ])
    def test_read_only_commands_allowed(self, cmd, tmp_path, monkeypatch):
        """Reading from a read-only path is fine — only mutation operators block."""
        self._rules(tmp_path, monkeypatch, {"readOnlyPaths": ["/readonly/"]})
        assert check_command_safety(cmd)["decision"] == "allow"

    @pytest.mark.parametrize("cmd", [
        # Operators newly caught after #164 (cli_safety adopted the hook's regex set)
        "chmod 777 /readonly/file",
        "chown root /readonly/file",
        "chgrp staff /readonly/file",
        "echo x >> /readonly/file",
        "tee -a /readonly/file",
        "perl -pi -e 's/x/y/g' /readonly/file",
        "awk -i inplace '/x/ {print}' /readonly/file",
        "truncate -s 0 /readonly/file",
        "unlink /readonly/file",
    ])
    def test_richer_mutation_operators_caught(self, cmd, tmp_path, monkeypatch):
        """After #164, cli_safety uses the same regex pattern set as the bash hook."""
        self._rules(tmp_path, monkeypatch, {"readOnlyPaths": ["/readonly/"]})
        assert check_command_safety(cmd)["decision"] == "block"
