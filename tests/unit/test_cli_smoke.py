"""Smoke tests for the CLI SSOT layer (agentwire/__main__.py).

These guard the NameError/typo/bad-help-string bug class that crashes commands
without any handler ever running — e.g. the bare ``%`` in an argparse help
string that broke every command on Python 3.14 (see issue #486). They build the
full parser and invoke ``--help`` for every registered subcommand, asserting the
help renders cleanly (argparse exits 0) rather than raising.
"""

import argparse


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




def test_every_subcommand_help_renders(capsys):
    """`agentwire <subcmd...> --help` renders cleanly and exits 0, for every subcommand.

    Help-string formatting (e.g. a stray ``%`` that argparse treats as a
    printf token) raises here at the parser layer, before any handler runs.
    One test over the whole tree: the failure lists every broken path.
    """
    from agentwire.__main__ import build_parser

    parser = build_parser()
    broken = {}
    for path in _all_paths():
        try:
            parser.parse_args([*path, "--help"])
            broken[" ".join(path)] = "did not exit"
        except SystemExit as exc:
            out = capsys.readouterr().out
            if exc.code != 0 or not out.strip():
                broken[" ".join(path)] = f"exit={exc.code} help_printed={bool(out.strip())}"
        except Exception as exc:  # noqa: BLE001 - the bug class is any crash at the parser layer
            broken[" ".join(path)] = repr(exc)
    assert not broken, broken


# --- #827: --kind reviewer is a valid argparse choice everywhere --kind is
# accepted (new/worktree/session-defaults) — a bad choice would raise
# SystemExit(2) at the parser layer before any handler runs.

