"""Path resolution / cwd lane of the control-plane gate (#1132).

Every statically decidable way of naming a protected file by something other
than its absolute path: a directory set by the same command (``d=<dir>; cd $d``,
``env -C``), an alias made before the hook ran (``ln -s``, a hardlink, a
symlinked directory), and a diff that names its own targets (``patch``,
``git apply``). The rest fails closed with an ASK that bypass cannot demote.

Each test is one operation with a loop over its spellings: a failure lists
every spelling that went the wrong way at once. The decisions are measured
against the bundled rules, never the machine's live copies.
"""

import os
import shlex
from pathlib import Path

import pytest

from agentwire.safety import _core
from agentwire.safety._core import check_command, load_config

pytestmark = pytest.mark.real_agentwire_home

REPO = Path(__file__).resolve().parent.parent.parent
RULES_DIR = REPO / "agentwire" / "hooks" / "damage-control" / "rules"
HOME = "/home/cptest"
CTL = f"{HOME}/.agentwire/damagecontrol.yml"
AW = f"{HOME}/.agentwire"


@pytest.fixture(autouse=True)
def _fixed_home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


@pytest.fixture(scope="module")
def cfg():
    c = load_config(RULES_DIR, REPO / "agentwire" / "tooldefs")
    c["safety"] = {"enabled": True, "disabled_rules": [], "unattended_allow": []}
    return c


def _decide(command, cfg):
    return check_command(command, cfg)["decision"]


def _wrongly(commands, want, cfg):
    """Commands whose decision is not ``want`` (``refuse`` = block or ask)."""
    bad = []
    for cmd in commands:
        d = _decide(cmd, cfg)
        ok = d in ("block", "ask") if want == "refuse" else d == want
        if not ok:
            bad.append((d, cmd))
    return bad


# ---- #15: false blocks -------------------------------------------------------


def test_rsync_writes_only_its_destination(cfg):
    sources = [
        f"rsync -a {CTL} host:x",
        f"rsync -av {CTL} host:/backup/",
        f"cd {AW} && rsync -a damagecontrol.yml host:x",
        f"rsync -a --exclude=x {CTL} host:x",
    ]
    destinations = [
        f"rsync -a src {CTL}",
        f"rsync -a src {CTL} --delete",
        f"rsync -a src {CTL} --exclude=x",
        f"rsync -a src1 src2 {CTL}",
        f"cd {AW} && rsync -a src damagecontrol.yml",
        f"rsync -a {CTL} {CTL}",
    ]
    assert not _wrongly(sources, "allow", cfg)
    assert not _wrongly(destinations, "refuse", cfg)


def test_locale_quoted_heredoc_delimiter_is_a_quoted_heredoc(cfg):
    body = f"cd {AW} && touch damagecontrol.yml"
    data = [f'cat <<$"EOF"\n{body}\nEOF', f'cat <<-$"EOF"\n\t{body}\n\tEOF', f"cat <<'EOF'\n{body}\nEOF"]
    runs = [f'bash <<$"EOF"\ntouch {CTL}\nEOF', f'bash <<-$"EOF"\n\ttouch {CTL}\n\tEOF']
    unquoted_is_still_scanned = [f"cat <<EOF\n{body}\nEOF"]
    assert not _wrongly(data, "allow", cfg)
    assert not _wrongly(runs, "refuse", cfg)
    assert _decide(unquoted_is_still_scanned[0], cfg) == "allow"  # data too; the carve-out is about the quote, not the `$`


def test_glob_with_a_character_class_is_not_a_literal_name(cfg):
    harmless = ["echo [a-z]*; echo hi | sh", "echo [0-9]*; echo hi | sh", "ls [a-f]?? | sh"]
    names_it = [
        f"cd {AW} && rm d*ntrol.yml",
        f"cd {AW} && rm dam[a]gecontrol.yml",
        f"rm {AW}/damage[c]ontrol.yml",
    ]
    assert not _wrongly(harmless, "allow", cfg)
    assert not _wrongly(names_it, "refuse", cfg)


# ---- #16: a directory the scanner can't see -----------------------------------


def test_directory_set_by_the_same_command_is_resolved(cfg):
    names_it = [
        f"d={AW}; cd $d && touch damagecontrol.yml",
        f'd={AW}; cd "$d" && touch damagecontrol.yml',
        f"d={AW}; cd ${{d}} && touch damagecontrol.yml",
        f"export d={AW}; cd $d && touch damagecontrol.yml",
        f"d={AW}\ncd $d && touch damagecontrol.yml",
        f"env -C {AW} touch damagecontrol.yml",
        f"env --chdir={AW} touch damagecontrol.yml",
        f"env -i -C {AW} touch damagecontrol.yml",
        f"env FOO=1 -C {AW} touch damagecontrol.yml",
        f"A=${{x:-\n}} cd {AW} && touch damagecontrol.yml",
        f'A="a b" cd {AW} && touch damagecontrol.yml',
        f"A='a b' B=1 cd {AW} && touch damagecontrol.yml",
    ]
    elsewhere = [
        "d=/tmp; cd $d && touch damagecontrol.yml",
        f"d={AW}; d=/tmp; cd $d && touch damagecontrol.yml",
        "env -C /tmp touch damagecontrol.yml",
        f"env -C /tmp make -C {AW} clean",
        f"make -C {AW} clean",
        f"d={AW}; cd $d && cat damagecontrol.yml",
    ]
    assert not _wrongly(names_it, "refuse", cfg)
    assert not _wrongly(elsewhere, "allow", cfg)


def test_unresolvable_cd_then_a_protected_name_is_an_unbypassable_ask(cfg):
    asks = [
        f"cd {AW}; cd /tmp; cd - && touch damagecontrol.yml",
        f"pushd {AW} && popd && touch damagecontrol.yml",
        "cd - && touch damagecontrol.yml",
        "cd $x && touch damagecontrol.yml",
        "cd $x && rm -f damagecontrol.yml",
        "cd $x && cp /tmp/a scheduler.yaml",
    ]
    bad = []
    for cmd in asks:
        r = check_command(cmd, cfg)
        if (r["decision"], r.get("unverifiable"), r.get("id")) != ("ask", True, "core.control-plane-target"):
            bad.append((r["decision"], r.get("id"), cmd))
    assert not bad, bad


def test_unresolvable_cd_is_quiet_for_everything_else(cfg):
    fine = [
        "cd - && ls",
        "cd $x && cat damagecontrol.yml",
        "cd $x && touch notes.txt",
        "cd $x && sed -i s/a/b/ settings.json",
        "cd $x && patch -p1 < x.diff",
        "cd /tmp/work; cd - && touch damagecontrol.yml.bak",
    ]
    assert not _wrongly(fine, "allow", cfg)


def test_decision_inside_a_wrapper_equals_the_top_level_decision(cfg):
    inner = [
        f"d={AW}; cd $d && touch damagecontrol.yml",
        "cd - && touch damagecontrol.yml",
        f"env -C {AW} touch damagecontrol.yml",
        f"cd {AW} && patch -p0 < x.diff",
        "cd $x && touch notes.txt",
        "cd /tmp && patch -p1 < x.diff",
    ]
    wrappers = [
        lambda c: f"bash -c {shlex.quote(c)}",
        lambda c: f"({c})",
        lambda c: f"{{ {c}; }}",
        lambda c: f"if true; then {c}; fi",
        lambda c: f"true && {c}",
        lambda c: f"ssh host {shlex.quote(c)}",
    ]
    bad = []
    for cmd in inner:
        top = _decide(cmd, cfg)
        for wrap in wrappers:
            wrapped = _decide(wrap(cmd), cfg)
            if wrapped != top:
                bad.append((top, wrapped, wrap(cmd)))
    assert not bad, bad


# ---- #17: patch / git apply ---------------------------------------------------


def test_diff_applied_in_a_protected_directory_asks(cfg):
    asks = [
        f"cd {AW} && patch -p0 < x.diff",
        f"cd {HOME}/.claude && git apply x.diff",
        f"git -C {HOME}/.claude am x.mbox",
        f"git -C {AW} apply x.diff",
        f"patch -d {AW} -p0 < x.diff",
        f"patch --directory={AW} -p0 < x.diff",
        f"cd {HOME} && patch -p1 < x.diff",
        f"cd {HOME}/.claude/hooks && git apply x.diff",
    ]
    quiet = [
        "cd /tmp/repo && patch -p1 < x.diff",
        "git -C /tmp/repo apply x.diff",
        "git apply x.diff",
        f"cd {AW} && patch --dry-run -p0 < x.diff",
        f"cd {AW} && git apply --check x.diff",
        f"cd {AW} && git status",
        f"cd {AW} && git diff > /tmp/x.diff",
    ]
    assert not _wrongly(asks, "ask", cfg)
    assert not _wrongly(quiet, "allow", cfg)


# ---- #20: brace expansion past the cap ----------------------------------------


def test_brace_expansion_past_the_cap_cannot_hide_the_control_plane(cfg):
    groups = "{a,b}" * 10
    over = [
        f"touch {AW}/{groups}damagecontrol.yml",
        f"cd {AW} && touch {groups}damagecontrol.yml",
        f"rm {HOME}/.claude/settings.json {groups}",
    ]
    under = [f"touch /tmp/{'{a,b}' * 4}x", f"ls {AW}/{'{a,b}' * 4}"]
    off = dict(cfg, safety={"enabled": False, "disabled_rules": []})
    refused = [c for c in over if check_command(c, off)["decision"] not in ("ask", "block")]
    assert not refused, refused  # kill-switch exempt, like the rest of the protected tier
    assert not _wrongly(under, "allow", cfg)


# ---- #26: aliases ------------------------------------------------------------


def test_a_link_to_the_control_plane_is_refused(cfg):
    refused = [
        f"ln -s {CTL} /tmp/lnk",
        f"ln {CTL} /tmp/hl",
        f"ln -sf {CTL} /tmp/lnk",
        f"ln -s {AW} /tmp/d",
        f"ln -s {HOME}/.claude/hooks /tmp/d",
        f"cd {AW} && ln -s damagecontrol.yml /tmp/lnk",
        f"ln -s /tmp/a {CTL}",
    ]
    allowed = [
        "ln -s /tmp/a /tmp/b",
        f"ln -s {AW}/sessions /tmp/d",
        f"ln -s {HOME}/.claude/projects /tmp/d",
        f"ls -l {CTL}",
    ]
    assert not _wrongly(refused, "refuse", cfg)
    assert not _wrongly(allowed, "allow", cfg)


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".agentwire").mkdir(parents=True)
    (home / ".agentwire" / "damagecontrol.yml").write_text("x")
    (home / ".agentwire" / "notes.txt").write_text("x")
    work = tmp_path / "work"
    work.mkdir()
    (work / "symlink").symlink_to(home / ".agentwire" / "damagecontrol.yml")
    os.link(home / ".agentwire" / "damagecontrol.yml", work / "hardlink")
    (work / "dirlink").symlink_to(home / ".agentwire")
    (work / "benign").symlink_to(home / ".agentwire" / "notes.txt")
    (work / "plain").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    _core._protected_inodes.cache_clear()
    yield home, work
    _core._protected_inodes.cache_clear()


def test_a_path_that_already_aliases_the_control_plane_is_judged_as_the_file(real_home, cfg):
    _home, work = real_home
    writes = [
        f"echo x > {work}/symlink",
        f"echo x >> {work}/symlink",
        f"rm {work}/symlink",
        f"touch {work}/dirlink/damagecontrol.yml",
        f"cp /tmp/a {work}/dirlink/damagecontrol.yml",
        f"echo x > {work}/hardlink",
        f"tee {work}/hardlink",
    ]
    fine = [
        f"cat {work}/symlink",
        f"cat {work}/hardlink",
        f"cat {work}/dirlink/damagecontrol.yml",
        f"echo x > {work}/plain",
        f"echo x > {work}/benign",
        f"echo x > {work}/not-yet-created",
        f"ls {work}/dirlink",
    ]
    assert not _wrongly(writes, "refuse", cfg)
    assert not _wrongly(fine, "allow", cfg)
