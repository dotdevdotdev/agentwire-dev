"""Verify the damage-control hook scripts are in sync with ``agentwire/safety/_core.py``.

The hooks are PEP 723 standalone scripts that inline the contents of
``_core.py`` between BEGIN/END markers. ``scripts/regen_damage_control_hooks.py``
keeps them in sync. This test fails loudly if anyone edits ``_core.py`` and
forgets to regenerate.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
CORE_PATH = REPO / "agentwire" / "safety" / "_core.py"
HOOKS_DIR = REPO / "agentwire" / "hooks" / "damage-control"

HOOK_FILES = [
    "bash-tool-damage-control.py",
    "edit-tool-damage-control.py",
    "write-tool-damage-control.py",
]

BEGIN_MARKER = "# === BEGIN GENERATED FROM agentwire/safety/_core.py ==="
END_MARKER = "# === END GENERATED ==="


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_hook_inlines_current_core(hook_name):
    """Each hook must inline the current ``_core.py`` content between markers."""
    hook_path = HOOKS_DIR / hook_name
    assert hook_path.exists(), f"Hook script missing: {hook_path}"

    hook_text = hook_path.read_text()
    assert BEGIN_MARKER in hook_text, f"{hook_name} is missing BEGIN_MARKER"
    assert END_MARKER in hook_text, f"{hook_name} is missing END_MARKER"

    _, _, after_begin = hook_text.partition(BEGIN_MARKER)
    inlined, _, _ = after_begin.partition(END_MARKER)

    core_text = CORE_PATH.read_text()

    assert inlined.strip() == core_text.strip(), (
        f"{hook_name} is out of sync with {CORE_PATH.relative_to(REPO)}.\n"
        f"Run:  uv run python scripts/regen_damage_control_hooks.py"
    )


def test_each_hook_has_main_after_marker():
    """Sanity: hook-specific main() must live AFTER the END marker."""
    for hook_name in HOOK_FILES:
        text = (HOOKS_DIR / hook_name).read_text()
        _, _, after_end = text.partition(END_MARKER)
        assert "def main()" in after_end, (
            f"{hook_name}: main() must be defined after the END marker, "
            f"not inside the generated section"
        )
        assert "if __name__" in after_end, (
            f"{hook_name}: __main__ guard must be after the END marker"
        )
