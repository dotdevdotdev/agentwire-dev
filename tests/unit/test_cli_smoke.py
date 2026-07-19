"""Smoke tests for the CLI SSOT layer (agentwire/__main__.py).

These guard the NameError/typo/bad-help-string bug class that crashes commands
without any handler ever running — e.g. the bare ``%`` in an argparse help
string that broke every command on Python 3.14 (see issue #486). They build the
full parser and invoke ``--help`` for every registered subcommand, asserting the
help renders cleanly (argparse exits 0) rather than raising.
"""

import argparse

import pytest


def _iter_subcommand_paths(parser, prefix=()):
    """Yield the argv path to every (sub)command in the parser tree.

    Each yielded path is a tuple like ``("portal",)`` or ``("safety", "tooldefs")``
    that can be passed to ``parse_args`` followed by ``--help``.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                path = prefix + (name,)
                yield path
                yield from _iter_subcommand_paths(subparser, path)


def test_build_parser_constructs():
    """The top-level parser builds without raising (catches bad help strings)."""
    from agentwire.__main__ import build_parser

    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
    # At least the headline subcommands should be registered.
    top_level = {p[0] for p in _iter_subcommand_paths(parser) if len(p) == 1}
    assert {"portal", "new", "say", "scheduler"} <= top_level


def _all_paths():
    from agentwire.__main__ import build_parser

    return list(_iter_subcommand_paths(build_parser()))


def test_at_least_one_subcommand_discovered():
    assert len(_all_paths()) > 0


@pytest.mark.parametrize(
    "path",
    _all_paths(),
    ids=lambda p: " ".join(p),
)
def test_subcommand_help_renders(path, capsys):
    """`agentwire <subcmd...> --help` renders cleanly and exits 0.

    Help-string formatting (e.g. a stray ``%`` that argparse treats as a
    printf token) raises here at the parser layer, before any handler runs.
    """
    from agentwire.__main__ import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([*path, "--help"])
    assert exc_info.value.code == 0
    # Help should actually print something.
    assert capsys.readouterr().out.strip()


# --- #827: --kind reviewer is a valid argparse choice everywhere --kind is
# accepted (new/worktree/session-defaults) — a bad choice would raise
# SystemExit(2) at the parser layer before any handler runs.

@pytest.mark.parametrize("argv", [
    ["new", "-s", "proj", "--kind", "reviewer"],
    ["worktree", "some-name", "--kind", "reviewer"],
    ["session-defaults", "--kind", "reviewer"],
])
def test_kind_reviewer_is_accepted(argv):
    from agentwire.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.kind == "reviewer"
