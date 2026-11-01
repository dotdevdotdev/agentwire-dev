"""Verify the damage-control hook scripts are in sync with the ``agentwire/safety/`` engine.

The hooks are PEP 723 standalone scripts that inline the engine modules
(``regen_damage_control_hooks.ENGINE_SOURCES``) between BEGIN/END markers.
``scripts/regen_damage_control_hooks.py`` keeps them in sync. This test fails loudly
if anyone edits an engine module and forgets to regenerate.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location(
    "regen_damage_control_hooks", REPO / "scripts" / "regen_damage_control_hooks.py"
)
regen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(regen)
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
    engine = regen.engine_text()
    for hook_name in HOOK_FILES:
        hook_text = (HOOKS_DIR / hook_name).read_text()
        _, _, after_begin = hook_text.partition(BEGIN_MARKER)
        inlined, found_end, after_end = after_begin.partition(END_MARKER)
        if not found_end or inlined.strip() != engine.strip() or "def main()" not in after_end:
            stale.append(hook_name)
    assert not stale, (
        f"{stale} out of sync with the agentwire/safety engine modules.\n"
        f"Run:  uv run python scripts/regen_damage_control_hooks.py"
    )
