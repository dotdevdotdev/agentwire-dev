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
import random
import re
import shlex
import time
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
    harmless = ["echo [a-z]*; echo hi | sh", "echo [0-9]*; echo hi | sh", "ls [a-f]??; echo ls | sh"]
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


# ---- #21: the glob-word scan is linear ---------------------------------------

# The scan this replaced, verbatim: a regex that retries from every offset of a
# long run of non-delimiters. Kept as the oracle for the decision-preserving
# claim and as the measure of what the replacement avoids.
_OLD_GLOB_TOKEN = re.compile(r"(?<![\w$'\"])[^\s'\"`;|&<>()]*(?:[*?\[]|\$(?:\w|\{))[^\s'\"`;|&<>()]*")


def test_glob_words_are_the_same_words_the_old_regex_found():
    rng = random.Random(1132)
    alphabet = ["a", "b", "_", "$", "*", "?", "[", "]", "{", "/", ".", " ", "'", '"', ";", "(", "é", "-", "x$", "$x"]
    differing = []
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        old = [m.group(0) for m in _OLD_GLOB_TOKEN.finditer(text)]
        if old != list(_core._glob_tokens(text)):
            differing.append(text)
    assert not differing, differing[:5]


def test_glob_word_scan_does_not_retry_from_every_offset_of_a_long_run():
    runs = ["../" * 7000, "\\" * 20000, "{a," * 7000, "x" * 20000]
    slow = []
    for run in runs:
        start = time.perf_counter()
        list(_core._glob_tokens("ls " + run))
        took = time.perf_counter() - start
        if took > 0.5:  # the old scan took seconds on each; this takes milliseconds
            slow.append((run[:6], round(took, 2)))
    assert not slow, slow


# ---- review round 1: allowance, redirects, per-segment diffs, wrapper gaps ----

CARRIERS = [
    lambda c: c,
    lambda c: f"bash -c {shlex.quote(c)}",
    lambda c: f"({c})",
    lambda c: f"{{ {c}; }}",
    lambda c: f"ssh host {shlex.quote(c)}",
    lambda c: f"timeout 5 bash -c {shlex.quote(c)}",
]


def _carried_wrongly(spellings, want, cfg):
    bad = []
    for spelling in spellings:
        for carry in CARRIERS:
            cmd = carry(spelling)
            d = _decide(cmd, cfg)
            if (d in ("block", "ask")) != (want == "refuse"):
                bad.append((d, cmd))
    return bad


def test_a_home_under_a_default_allowance_does_not_waive_the_ask(tmp_path, monkeypatch, cfg):
    home = tmp_path / "h"  # pytest tmp dirs sit under /tmp or /private/var: both are default allowances
    (home / ".agentwire").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for allowed in ("/tmp/*", f"{home}/*", f"{tmp_path}/*"):
        local = dict(cfg, allowedPaths=[{"path": allowed, "allow": "all"}])
        asks = ["cd - && touch damagecontrol.yml", "cd $x && touch damagecontrol.yml", f"cd {home}/.agentwire && patch -p0 < x.diff"]
        assert not _wrongly(asks, "refuse", local), allowed


def test_a_redirect_on_the_cd_does_not_hide_the_directory(cfg):
    spellings = [
        f"cd {AW} >/dev/null && touch damagecontrol.yml",
        f"cd {AW} 2>/dev/null && touch damagecontrol.yml",
        f"cd {AW} > /dev/null 2>&1 && touch damagecontrol.yml",
        f"cd {AW} &>/dev/null && touch damagecontrol.yml",
        f"d={AW}; cd $d 2>/dev/null && touch damagecontrol.yml",
        f"pushd {AW} >/dev/null; touch damagecontrol.yml",
        "cd $x 2>/dev/null && touch damagecontrol.yml",
        "cd - >/dev/null && touch damagecontrol.yml",
        "cd .. && touch damagecontrol.yml",
    ]
    controls = ["cd /tmp >/dev/null && touch damagecontrol.yml", "cd $x 2>/dev/null && touch notes.txt"]
    assert not _carried_wrongly(spellings, "refuse", cfg)
    assert not _wrongly(controls, "allow", cfg)


def test_a_read_only_flag_excuses_only_its_own_command(cfg):
    applies = [
        f"cd {AW} && git apply --stat x.diff; git apply x.diff",
        f"cd {AW} && patch --dry-run a; patch b",
        f"cd {AW} && patch -p0 < x.diff # --dry-run",
        f"cd {AW} && patch -p0 < x.diff | tee f --stat",
        f'cd {AW} && git apply "x --check .diff"',
        f"cd {AW} && /usr/bin/patch -p0 < x.diff",
        f"cd {AW} && /usr/bin/git apply x.diff",
    ]
    reads = [f"cd {AW} && patch --dry-run -p0 < x.diff", f"cd {AW} && git apply --check x.diff", f"cd {AW} && git apply --stat x.diff"]
    assert not _carried_wrongly(applies, "refuse", cfg)
    assert not _wrongly(reads, "allow", cfg)


def test_wrapper_payloads_are_found_through_options_and_double_quote_escapes(cfg):
    inner = f"d={AW}; cd $d && touch damagecontrol.yml"
    spellings = [
        'bash -c "d=%s; cd \\$d && touch damagecontrol.yml"' % AW,
        f"bash -c -- {shlex.quote(inner)}",
        f"ssh host -t {shlex.quote(inner)}",
        f"timeout 5 bash -c {shlex.quote(inner)}",
        f"env -C /tmp sh -c {shlex.quote(inner)}",
        f"sudo -u root bash -lc {shlex.quote(inner)}",
    ]
    assert not _wrongly(spellings, "refuse", cfg)


def test_env_clusters_and_nesting_resolve_the_directory(cfg):
    spellings = [
        f"env -iC {AW} touch damagecontrol.yml",
        f"env -C /tmp env -C {AW} touch damagecontrol.yml",
        "env -C / env -C home/cptest/.agentwire touch damagecontrol.yml",
    ]
    controls = [f"env -C {AW} env -C /tmp touch damagecontrol.yml", "env -iC /tmp touch damagecontrol.yml"]
    assert not _wrongly(spellings, "refuse", cfg)
    assert not _wrongly(controls, "allow", cfg)


def test_wrapper_payload_with_a_resolvable_directory_blocks_rather_than_asks(cfg):
    """A resolved directory is a BLOCK; falling back to the unresolvable-cd ask means the payload was mis-read."""
    spellings = [
        'bash -c "d=%s; cd \\$d && touch damagecontrol.yml"' % AW,
        f"ssh host -t {shlex.quote(f'cd {AW} && touch damagecontrol.yml')}",
    ]
    not_blocked = [(_decide(c, cfg), c) for c in spellings if _decide(c, cfg) != "block"]
    assert not not_blocked, not_blocked


def test_a_prefix_wrapper_does_not_hide_the_diff_applier(cfg):
    verbs = ["patch -p0 < x.diff", "git apply x.diff", "git -C . am x.mbox", "/usr/bin/patch -p0 < x.diff"]
    prefixes = ["", "sudo ", "sudo -u root ", "nohup ", "env ", "env A=1 ", "xargs ", "timeout 5 ", "command ", "nice -n 5 ", "sudo /usr/bin/", "time "]
    bad = []
    for verb in verbs:
        for prefix in prefixes:
            if prefix.endswith("/") and not verb.startswith(("patch", "git")):
                continue
            body = verb if not prefix.endswith("/") else prefix + verb
            cmd = f"cd {AW} && {body if prefix.endswith('/') else prefix + body}"
            base = f"cd {AW} && {verb}"
            if _decide(cmd, cfg) != _decide(base, cfg):
                bad.append((_decide(base, cfg), _decide(cmd, cfg), cmd))
    assert not bad, bad
    reads = [f"cd {AW} && sudo patch --dry-run -p0 < x.diff", f"cd {AW} && nohup git apply --check x.diff"]
    assert not _wrongly(reads, "allow", cfg)


def test_cd_with_input_redirects_keeps_its_directory(cfg):
    spellings = [f"cd {AW} <&- && touch damagecontrol.yml", f"cd {AW} <&0 && touch damagecontrol.yml", f"cd {AW} </dev/null && touch damagecontrol.yml"]
    assert not _carried_wrongly(spellings, "refuse", cfg)


WRAPPER_FORMS = [
    "nice -n5", "nice -n 5", "nice --adjustment=5", "nice --adjustment 5",
    "ionice -c2", "ionice -c 2", "ionice -c2 -n4", "ionice -c 2 -n 4",
    "timeout -s9 5", "timeout -s 9 5", "timeout -k 3 5", "timeout -k3 5", "timeout --signal=KILL 5", "timeout --signal KILL 5",
    "env -uVAR", "env -u VAR", "env -C /tmp", "env -S 'A=1'", "env -iu VAR",
    "sudo -uroot", "sudo -u root", "sudo -E", "sudo -H", "sudo -E -u root -H",
    "stdbuf -oL", "stdbuf -o L", "stdbuf -oL -eL",
    "taskset -c0", "taskset -c 0", "taskset 0xff", "chrt -f 1", "chrt -r 10",
    "xargs -n1", "xargs -n 1", "nohup", "command", "time -p",
    "sudo -u root nice -n5 timeout -s9 5",
]


def test_wrapper_option_forms_do_not_hide_the_diff_applier(cfg):
    bad = []
    for verb in ("patch -p0 < x.diff", "git apply x.diff", "git -C . am x.mbox"):
        top = _decide(f"cd {AW} && {verb}", cfg)
        for form in WRAPPER_FORMS:
            cmd = f"cd {AW} && {form} {verb}"
            if _decide(cmd, cfg) != top:
                bad.append((top, _decide(cmd, cfg), cmd))
    assert not bad, bad
    benign = [f"cd {AW} && {form} patch --dry-run -p0 < x.diff" for form in WRAPPER_FORMS]
    assert not _wrongly(benign, "allow", cfg)
