"""The host-supplied half of the damage-control engine (#1209 M3, section 4.2).

The engine knows what is dangerous in general; a HOST (an application that embeds it)
adds what is dangerous *around itself*: its own config and control-plane files, the
commands that must never run through an agent, the env vars its scheduler stamps, the
rule ids it is willing to grant unattended. A ``Profile`` is that whole surface, as
plain data, so the engine has no host names in its logic.

``Profile`` only ever ADDS. The generic control plane (the project ``.damagecontrol.yml``,
``~/.claude/settings.json``, ``~/.claude/hooks/*``, the host's policy file) is a kernel
that ``install_profile`` (in ``_core.py``) puts first and a profile cannot remove or
reorder: the control-plane and protected-command checks run before the kill switch and
nothing a profile supplies can loosen them.

Standard library only, no imports from this package: it is inlined into the hooks next to
``_core.py`` (``scripts/regen_damage_control_hooks.py``) and must work in either place.
Plain ``NamedTuple``s, not dataclasses: a dataclass resolves its annotations through
``sys.modules[cls.__module__]``, which a hook loaded by path (the tests, ``runpy``) lacks.
"""

from typing import NamedTuple, Tuple


class GroupSpec(NamedTuple):
    """One rule group or opt-in pack: which bundled files it owns (#1141)."""

    name: str
    kind: str  # "group" | "pack"
    description: str
    rules: Tuple[str, ...] = ()
    tooldefs: Tuple[str, ...] = ()
    kernel: bool = False
    binaries: Tuple[str, ...] = ()  # vendor CLIs whose presence suggests a pack
    # Rule files shipped by a host layer (``Profile.extra_rules_dirs``). Owned by the
    # SAME entry as the generic file they were split from, so an opt-out or kernel pin
    # still covers every entry that group covered before the split.
    layer_rules: Tuple[str, ...] = ()


class McpCommandTemplate(NamedTuple):
    """How an outbound MCP tool maps onto the shell command it runs under the hood.

    The synthesized command must match the same rule the Bash shell-out matches. The
    body is omitted on purpose (privacy, audit clarity): rules key on the verb.
    ``argv`` is the fixed prefix; each ``flags`` entry is ``(tool_input key, flag)`` —
    a list value repeats the flag, an empty value omits it.
    """

    tool: str  # short tool name, e.g. "email_send" (the part after the last "__")
    argv: Tuple[str, ...]
    flags: Tuple[Tuple[str, str], ...] = ()


class Profile(NamedTuple):
    """Everything a host adds to the generic engine. Defaults are the generic engine."""

    name: str = "generic"

    # --- config home ------------------------------------------------------
    # Where the host keeps its protected policy file (``damagecontrol.yml``) and its
    # rule/tooldef overrides. ``config_dir_env`` (if set in the environment) relocates
    # the override dirs only — never the policy file, which is always protected.
    config_dir: str = "~/.damage-control"
    config_dir_env: str = "DAMAGE_CONTROL_HOME"

    # --- protected control plane (additive) -------------------------------
    protected_paths: Tuple[str, ...] = ()
    # Names a program may use for a control-plane location it builds at run time.
    control_plane_words: Tuple[str, ...] = ()
    # Extra regex alternatives for directories agents write by design (matched whole-segment).
    benign_path_patterns: Tuple[str, ...] = ()
    # Fixed stand-in for ``$HOME`` when judging what a ``~``-anchored entry covers.
    home_alias: str = "/home/damage-control-canonical"

    # --- protected commands (additive; no allowlist, no escape hatch, no kill switch) ---
    protected_commands: Tuple[Tuple[str, str], ...] = ()

    # --- unattended / headless --------------------------------------------
    # The mechanism and scope evaluation live in the engine; the host names the env vars
    # its scheduler stamps and the rule ids it grants by default (the engine ships none).
    unattended_env: str = "DAMAGE_CONTROL_UNATTENDED"
    unattended_allow_env: str = "DAMAGE_CONTROL_UNATTENDED_ALLOW"
    default_unattended_allow: Tuple[str, ...] = ()
    # First half of the sentence telling a blocked unattended run how to get a grant;
    # ``{rule_id}`` is filled in. Empty = say nothing.
    unattended_grant_hint: str = ""
    # argv prefix run detached when an unattended action is blocked; the engine appends
    # ``--reason R --rule-id ID --command C``. Empty = no notification.
    unattended_block_notifier: Tuple[str, ...] = ()

    # --- extra rules ------------------------------------------------------
    # Directories (relative to the bundled hook dir) merged after the generic rules.
    extra_rules_dirs: Tuple[str, ...] = ()

    # --- MCP --------------------------------------------------------------
    mcp_command_templates: Tuple[McpCommandTemplate, ...] = ()

    # --- groups / packs ---------------------------------------------------
    groups: Tuple[GroupSpec, ...] = ()
    # ``{existing group name: layer rule files}`` the host adds to generic groups.
    group_layer_rules: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    default_packs: Tuple[str, ...] = ()

    def layer_rules_for(self, group: str) -> Tuple[str, ...]:
        return dict(self.group_layer_rules).get(group, ())

    def template_for(self, tool_name: str):
        short = tool_name.split("__")[-1]
        for t in self.mcp_command_templates:
            if t.tool == short:
                return t
        return None


GENERIC = Profile()

__all__ = ["GENERIC", "GroupSpec", "McpCommandTemplate", "Profile"]
