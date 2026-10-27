"""Tests for ``agentwire diff`` — the structured diff behind the mobile Review
window (#484). Covers the unified-diff parser and the command's JSON shape +
base-resolution, with git fully mocked so no real repo is needed.
"""

import json
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


    def test_not_a_repo_errors(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(diff_cli, "_resolve_path", lambda s: tmp_path)
        monkeypatch.setattr(
            diff_cli, "_run_git",
            lambda p, a, **k: SimpleNamespace(returncode=128, stdout="", stderr="not a git repo"),
        )
        rc = diff_cli.cmd_diff(_ns())
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["success"] is False


