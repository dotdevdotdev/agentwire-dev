"""Verify the damage-control hook scripts are in sync with ``agentwire/safety/_core.py``.

The hooks are PEP 723 standalone scripts that inline the contents of
``_core.py`` between BEGIN/END markers. ``scripts/regen_damage_control_hooks.py``
keeps them in sync. This test fails loudly if anyone edits ``_core.py`` and
forgets to regenerate.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CORE_PATH = REPO / "agentwire" / "safety" / "_core.py"
HOOKS_DIR = REPO / "agentwire" / "hooks" / "damage-control"

HOOK_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
    "read-tool-damage-control.py",
    "mcp-tool-damage-control.py",
]

BEGIN_MARKER = "# === BEGIN GENERATED FROM agentwire/safety/_core.py ==="
END_MARKER = "# === END GENERATED ==="


def test_every_hook_inlines_the_current_core():
    """A hook executing a stale ``_core.py`` is the engine Claude Code actually runs."""
    stale = []
    for hook_name in HOOK_FILES:
        hook_text = (HOOKS_DIR / hook_name).read_text()
        _, _, after_begin = hook_text.partition(BEGIN_MARKER)
        inlined, found_end, after_end = after_begin.partition(END_MARKER)
        if not found_end or inlined.strip() != CORE_PATH.read_text().strip() or "def main()" not in after_end:
            stale.append(hook_name)
    assert not stale, (
        f"{stale} out of sync with {CORE_PATH.relative_to(REPO)}.\n"
        f"Run:  uv run python scripts/regen_damage_control_hooks.py"
    )
