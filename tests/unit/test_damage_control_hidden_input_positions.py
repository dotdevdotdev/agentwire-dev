"""A hidden-input form is judged the same behind any opener or wrapper as at the top level (#1144).

``sh <<< X``, ``sh <<EOF`` and ``sh < <(echo X)`` each hand a payload to a shell. Behind a
same-line compound keyword (``if true; then sh <<< X; fi``) or a wrapper's ``-c``
(``sudo bash -c 'sh <<< X'``) the decision must equal the bare form's.
"""

from pathlib import Path

import pytest

SAFETY = {"enabled": True, "disabled_rules": [], "unattended_allow": []}

# {s} is a payload-free shell invocation fed by the hidden-input form; {p} the payload.
FORMS = [
    "sh <<< '{p}'",
    "sh <<'EOF'\n{p}\nEOF",
    "sh < <(echo '{p}')",
]
OPENERS = [
    "{c}",
    "if true; then {c}; fi",
    "for i in 1; do {c}; done",
    "while true; do {c}; done",
    "until false; do {c}; done",
    "true; then {c}",
    "case x in x) {c};; esac",
    "{{ {c}; }}",
    "( {c} )",
    "! {c}",
    "time {c}",
    "echo a && {c}",
    "echo a; {c}",
]
WRAPPERS = ["sudo", "env", "nohup", "timeout 5", "env A=1", "nice -n 5", "command"]
PIPE_PRODUCERS_PAYLOADS = [("rm -rf xx", "block"), ("echo hi", "allow")]
PAYLOADS = [("rm -rf xx", "block"), ("echo hi", "allow")]


@pytest.fixture(scope="module")
def decide(bash_hook):
    repo = Path(__file__).resolve().parent.parent.parent
    config = bash_hook.load_config(repo / "agentwire/hooks/damage-control/rules", repo / "agentwire/tooldefs")
    assert not config.get("_parser_unavailable"), "rules failed to load"
    config = dict(config, safety=SAFETY)
    return lambda c: bash_hook.check_command(c, config)["decision"]


@pytest.fixture(autouse=True)
def _attended(monkeypatch):
    monkeypatch.delenv("AGENTWIRE_UNATTENDED", raising=False)


def test_openers_and_wrappers_match_top_level(decide):
    bad = []
    for payload, expected in PAYLOADS:
        for form in FORMS:
            # a heredoc's terminator needs its own line: only whole-line openers carry it
            inner = form.format(p=payload)
            assert decide(inner) == expected, ("top level", inner)
            for opener in OPENERS:
                if "\n" in inner and ("; " in opener.replace("{c}", "") or "&&" in opener):
                    continue
                cmd = opener.format(c=inner)
                if decide(cmd) != expected:
                    bad.append((cmd, expected))
            if "\n" in inner:
                continue
            for w in WRAPPERS:
                for q in ("'", '"'):
                    body = form.format(p=payload).replace("'", '"') if q == "'" else form.format(p=payload)
                    cmd = f"{w} bash -c {q}{body}{q}"
                    if decide(cmd) != expected:
                        bad.append((cmd, expected))
    assert not bad, bad


def test_wrapped_heredoc_in_dash_c(decide):
    bad = []
    for w in ["", *WRAPPERS]:
        cmd = f"{w} bash -c 'sh <<EOF\nrm -rf xx\nEOF\n'".strip()
        if decide(cmd) != "block":
            bad.append(cmd)
    assert not bad, bad


PIPE_PRODUCERS = ["cat <<< '{p}' | sh", "echo '{p}' | sh", "printf '%s' '{p}' | bash"]


def test_producer_pipe_to_shell_behind_openers_and_wrappers(decide):
    bad = []
    for payload, expected in PIPE_PRODUCERS_PAYLOADS:
        for form in PIPE_PRODUCERS:
            inner = form.format(p=payload)
            assert decide(inner) == expected, ("top level", inner)
            cmds = [o.format(c=inner) for o in OPENERS]
            cmds += [o.format(c=f"{w} {inner}") for w in WRAPPERS for o in OPENERS[:3]]
            bad += [(c, expected) for c in cmds if decide(c) != expected]
    assert not bad, bad
