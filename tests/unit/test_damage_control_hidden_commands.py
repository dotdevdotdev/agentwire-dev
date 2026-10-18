"""Hidden commands: text another program RUNS is judged as a command (batch 3 of the DC residuals).

``sh <<< 'X'``, ``cat <(X)``, ``sed 'e X'``, ``awk 'BEGIN{system("X")}'``,
``tar --to-command=X``, ``git fetch --upload-pack=X``, ``npm exec -c X`` and the
embedded-language interpreters all carry a second command the shell never
parses. The rule is ONE differential: a payload's decision inside any carrier
equals its decision alone. The controls price the other half — benign payloads
and the plain forms of each tool stay allowed.
"""

import shlex

import pytest

from agentwire.safety._core import has_command_position_eval

REPO_RULES = "agentwire/hooks/damage-control/rules"
CTL = "~/.agentwire/damagecontrol.yml"
SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}


@pytest.fixture(scope="module")
def cfg(bash_hook):
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent.parent
    config = bash_hook.load_config(repo / "agentwire/hooks/damage-control/rules", repo / "agentwire/tooldefs")
    assert not config.get("_parser_unavailable"), "rules failed to load"
    return dict(config, safety=SAFETY)


@pytest.fixture(autouse=True)
def _attended(monkeypatch):
    monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)


def _decide(bash_hook, cfg, command):
    return bash_hook.check_command(command, cfg)["decision"]


# Payloads with no quote characters of their own, so every carrier can embed them verbatim.
BLOCKED_PAYLOADS = [
    "rm -rf /x",
    "git push --force origin main",
    "git reset --hard HEAD~3",
    f"touch {CTL}",
]
BENIGN_PAYLOADS = ["ls -la /tmp", "git status", "echo hello"]

# Each carrier runs {p} (or a quoted {q} form) as a command, whatever the wrapped program is.
CARRIERS = [
    "sh <<< {q}",
    "bash <<'EOF'\n{p}\nEOF",
    "cat <({p})",
    "diff <(ls a) <({p})",
    "echo {q} | sh",
    "cat <<'EOF' | xargs -I{{}} sh -c '{{}}'\n{p}\nEOF",
    "echo {q} | xargs -I{{}} sh -c '{{}}'",
    "echo {q} | ssh host",
    "echo {q} | mosh host",
    "mosh host -- {p}",
    "sed -n '1e {p}' f",
    "sed -e 's/x/{p2}/e' f",
    "echo x | sed -ne '$e {p}'",
    "awk 'BEGIN{{system(\"{p}\")}}'",
    "awk 'BEGIN{{\"{p}\" | getline x}}'",
    "awk 'BEGIN{{print 1 | \"{p}\"}}'",
    "tar xf a.tar --to-command={q}",
    "tar xf a.tar --to-command {q}",
    "tar cf a.tar x --checkpoint=1 --checkpoint-action=exec={q}",
    "tar cf a.tar -I {q} x",
    "tar cf a.tar --use-compress-program={q} x",
    "git difftool -x {q}",
    "git difftool --extcmd={q}",
    "git fetch --upload-pack={q} origin",
    "git pull --upload-pack {q} origin",
    "git push --receive-pack={q} origin",
    "git clone -u {q} url",
    "git grep -O{q} foo",
    "git filter-branch --tree-filter {q} HEAD",
    "git submodule foreach {q}",
    "git bisect run {p}",
    "git config core.editor {q}",
    "git --no-pager -c user.name=x config --global core.pager {q}",
    "npm exec -c={q}",
    "npx -c {q}",
    "ssh -o ProxyCommand={q} host",
    "scp -S {q} a b:c",
    "sftp -o 'LocalCommand {p}' host",
    "sh -c 'sed -n \"1e {p}\" f'",
    "ssh host 'tar xf a.tar --to-command={q}'",
    "lua -e 'os.execute(\"{p}\")'",
    "luajit -e 'os.execute(\"{p}\")'",
    "Rscript -e 'system(\"{p}\")'",
    "R -e 'system(\"{p}\")'",
    "julia -e 'run(`{p}`)'",
    "groovy -e '\"{p}\".execute()'",
    "osascript -e 'do shell script \"{p}\"'",
    "tclsh <<< 'exec {p}'",
]


def _carrier(template, payload):
    return template.format(p=payload, q=shlex.quote(payload), p2=payload.replace("/", "\\/"))


def test_a_hidden_payload_decides_like_the_payload_alone(bash_hook, cfg):
    bad = []
    for payload in BLOCKED_PAYLOADS:
        plain = _decide(bash_hook, cfg, payload)
        assert plain == "block", (payload, plain)
        for template in CARRIERS:
            wrapped = _decide(bash_hook, cfg, _carrier(template, payload))
            if wrapped != plain:
                bad.append((template, payload, wrapped))
    assert not bad, bad


def test_benign_payloads_add_nothing_to_the_carrier(bash_hook, cfg):
    """A carrier can be ask-tier itself (``git pull``); a benign payload must not move that verdict."""
    bad = []
    for payload in BENIGN_PAYLOADS:
        assert _decide(bash_hook, cfg, payload) == "allow", payload
        for template in CARRIERS:
            baseline = _decide(bash_hook, cfg, _carrier(template, "true"))
            command = _carrier(template, payload)
            wrapped = _decide(bash_hook, cfg, command)
            if wrapped != baseline:
                bad.append((command, baseline, wrapped))
    assert not bad, bad


# The plain form of each tool, and the carrier syntax holding ordinary content, are NOT payloads.
PLAIN_FORMS = [
    "sed -n '1p' f", "echo x | sed 's/x/y/g'", "sed 's/e/E/' f", "sed -n '/re/p' f", "sed -i 's/a/b/' f",
    "sed -n 's/x/echo e/p' f", "awk '{print $1}' f", "awk -F: '{print $1 \"|\" $2}' f", "awk 'BEGIN{x=1||0}' f",
    "tar xf a.tar", "tar czf a.tgz dir", "tar cf a.tar -C /tmp x", "tar xf a.tar --checkpoint=100 --checkpoint-action=dot",
    "git fetch origin", "git difftool HEAD~1",
    "git config user.name x", "git config --global core.editor vim", "git config --get core.editor",
    "git clone https://example.com/r.git", "git grep -n foo", "git submodule update --init",
    "npm exec prettier -- --check .", "npx prettier --check .",
    "ssh -o StrictHostKeyChecking=no host ls", "ssh -o ProxyJump=jump host ls", "scp a host:/tmp/b",
    "sftp host", "sftp ~/.agentwire/damagecontrol.yml host:/x",
    "diff <(ls a) <(ls b)", "sh <<< 'echo hi'", "bash <<'EOF'\necho hi\nls\nEOF",
    "echo 'print(1)' | ssh host python3", "echo hi | mosh host", "mosh host",
    "lua -e 'print(1)'", "lua script.lua", "Rscript -e 'print(1)'", "julia -e 'println(1)'",
    "echo 'touch ~/.agentwire/damagecontrol.yml' | jq .",
    "echo x | xargs -I{} echo {}", "cat f | xargs -I{} sh -c 'echo {} done'",
    "tclsh script.tcl",
]


def test_plain_forms_and_ordinary_content_are_not_payloads(bash_hook, cfg):
    bad = [c for c in PLAIN_FORMS if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad


def test_remote_interpreter_stdin_is_judged_as_that_interpreters_program(bash_hook, cfg):
    blocked = [
        f"echo \"open('{CTL}','w')\" | ssh host python3",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua -",
        f"ssh host python3 <<'EOF'\nopen('{CTL}','w')\nEOF",
        f"echo \"open('{CTL}','w')\" | mosh host python3",
    ]
    allowed = [
        "echo 'print(1)' | ssh host python3",
        f"echo \"os.execute('touch {CTL}')\" | ssh host lua -e 'print(1)'",  # stdin is data: a program was given
    ]
    bad = [(c, "block") for c in blocked if _decide(bash_hook, cfg, c) != "block"]
    bad += [(c, "allow") for c in allowed if _decide(bash_hook, cfg, c) != "allow"]
    assert not bad, bad


def test_sftp_writes_its_last_operand_like_scp(bash_hook, cfg):
    blocked = [
        f"sftp host:/x {CTL}",
        f"sftp -P 22 host:/x {CTL}",
        f"sftp host <<< 'get /x {CTL}'",
        f"sftp host <<< '!touch {CTL}'",
        f"printf 'lcd ~/.agentwire\\nget /x damagecontrol.yml\\n' | sftp host",
    ]
    bad = [c for c in blocked if _decide(bash_hook, cfg, c) != "block"]
    assert not bad, bad


def test_embedded_interpreters_get_the_inline_program_net(bash_hook, cfg):
    names = {
        "lua": "-e", "luajit": "-e", "Rscript": "-e", "R": "-e", "julia": "-e", "groovy": "-e",
        "osascript": "-e",
    }
    bad = []
    for name, flag in names.items():
        for program in (f"open('{CTL}','w')", f"io.open('{CTL}','w')"):
            command = f"{name} {flag} {shlex.quote(program)}"
            if _decide(bash_hook, cfg, command) != "block":
                bad.append(command)
        if _decide(bash_hook, cfg, f"{name} {flag} 'print(1)'") != "allow":
            bad.append(f"{name} benign")
    assert not bad, bad


def test_the_outer_escape_hatch_still_ends_the_walk(bash_hook, cfg):
    """A hidden command is part of the command the user already waved through (kill switch / hatch)."""
    result = bash_hook.check_command("sh <<< 'rm -rf /x' # allow: scratch cleanup", cfg)
    assert result["decision"] == "allow" and result.get("escape")
    off = dict(cfg, safety=dict(SAFETY, enabled=False))
    assert bash_hook.check_command("sed -n '1e rm -rf /x' f", off)["decision"] == "allow"


def test_nesting_is_bounded_not_crashing(bash_hook, cfg):
    command = "rm -rf /x"
    for _ in range(12):
        command = f"sh <<< {shlex.quote(command)}"
    assert _decide(bash_hook, cfg, command) in {"block", "ask"}


# ---- launchers (#14) -------------------------------------------------------

LAUNCHER_DATA = [
    "parallel -j4 grep eval ::: f", "parallel grep eval {} ::: a b", "parallel -j 4 --colsep , grep eval ::: f",
    "flock /tmp/l grep eval f", "flock -w 5 /tmp/l grep eval f", "flock -n /tmp/l grep eval f",
    "docker run img grep eval f", "docker run --rm -e A=1 -v /a:/b img grep eval f",
    "docker exec c grep eval f", "podman run -it img grep eval f",
]
LAUNCHER_EXEC = [
    "parallel -j4 eval ::: f", "parallel 'eval {}' ::: f", "flock /tmp/l eval 'rm -rf /x'",
    "flock /tmp/l -c 'eval x'", "flock -w 5 /tmp/l eval x", "docker run img eval 'rm -rf /x'",
    "docker run --rm img sh -c 'eval x'", "docker exec c eval x", "docker run --entrypoint eval img x",
]


def test_known_launchers_see_the_verb_not_the_word_after_it():
    bad = [c for c in LAUNCHER_DATA if has_command_position_eval(c)]
    bad += [c for c in LAUNCHER_EXEC if not has_command_position_eval(c)]
    assert not bad, bad
