"""Tests for ``agentwire diff`` — the structured diff behind the mobile Review
window (#484). Covers the unified-diff parser and the command's JSON shape +
base-resolution, with git fully mocked so no real repo is needed.
"""

import json
import subprocess
from types import SimpleNamespace

from agentwire import diff_cli

SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@ def greet():
 import os
-print("old")
+print("new")
+print("added")
 done = True
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,1 @@
+hello
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 4444444..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""


def _ns(**kw):
    base = dict(session="proj/feat", base=None, json=True)
    base.update(kw)
    return SimpleNamespace(**base)


class TestParser:
    def test_files_and_counts(self):
        files, truncated = diff_cli.parse_unified_diff(SAMPLE_DIFF)
        assert not truncated
        assert [f["path"] for f in files] == ["foo.py", "new.txt", "gone.txt"]
        modified, added, deleted = files
        assert modified["status"] == "modified"
        assert modified["additions"] == 2 and modified["deletions"] == 1
        assert added["status"] == "added" and added["additions"] == 1
        assert deleted["status"] == "deleted" and deleted["deletions"] == 1

    def test_line_numbering(self):
        files, _ = diff_cli.parse_unified_diff(SAMPLE_DIFF)
        lines = files[0]["hunks"][0]["lines"]
        # context "import os" is line 1 on both sides
        ctx = lines[0]
        assert ctx["type"] == "context" and ctx["old_n"] == 1 and ctx["new_n"] == 1
        add = next(ln for ln in lines if ln["type"] == "add")
        assert add["content"] == 'print("new")' and add["new_n"] == 2

    def test_hunk_section_header(self):
        files, _ = diff_cli.parse_unified_diff(SAMPLE_DIFF)
        assert files[0]["hunks"][0]["section"] == "def greet():"

    def test_truncation(self):
        files, truncated = diff_cli.parse_unified_diff(SAMPLE_DIFF, max_lines=5)
        assert truncated


class TestBaseResolution:
    def test_uncommitted_uses_head(self, monkeypatch):
        monkeypatch.setattr(diff_cli, "_has_uncommitted", lambda p: True)
        assert diff_cli._resolve_base(diff_cli.Path("/x"), None) == "HEAD"

    def test_clean_tree_uses_origin_main(self, monkeypatch):
        monkeypatch.setattr(diff_cli, "_has_uncommitted", lambda p: False)
        monkeypatch.setattr(
            diff_cli, "_run_git", lambda p, a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        assert diff_cli._resolve_base(diff_cli.Path("/x"), None) == "origin/main"

    def test_explicit_base_wins(self):
        assert diff_cli._resolve_base(diff_cli.Path("/x"), "v1.2") == "v1.2"


class TestCommand:
    def test_json_output(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(diff_cli, "_resolve_path", lambda s: tmp_path)

        def fake_git(path, args, **kw):
            if args[:1] == ["rev-parse"]:
                return SimpleNamespace(returncode=0, stdout=".git", stderr="")
            if args[:1] == ["status"]:
                return SimpleNamespace(returncode=0, stdout=" M foo.py", stderr="")
            if args[:2] == ["diff", "--no-color"]:
                assert args[2] == "HEAD"  # uncommitted → HEAD
                return SimpleNamespace(returncode=0, stdout=SAMPLE_DIFF, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(diff_cli, "_run_git", fake_git)
        rc = diff_cli.cmd_diff(_ns())
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is True
        assert out["base"] == "HEAD"
        assert out["additions"] == 3 and out["deletions"] == 2
        assert len(out["files"]) == 3

    def test_unresolved_session_errors(self, monkeypatch, capsys):
        monkeypatch.setattr(diff_cli, "_resolve_path", lambda s: None)
        rc = diff_cli.cmd_diff(_ns())
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False

    def test_not_a_repo_errors(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(diff_cli, "_resolve_path", lambda s: tmp_path)
        monkeypatch.setattr(
            diff_cli, "_run_git",
            lambda p, a, **k: SimpleNamespace(returncode=128, stdout="", stderr="not a git repo"),
        )
        rc = diff_cli.cmd_diff(_ns())
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["success"] is False


def test_subprocess_import_present():
    # guard: cmd_diff relies on subprocess via _run_git
    assert hasattr(subprocess, "run")
