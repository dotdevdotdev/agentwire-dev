"""Rule groups and packs: which bundled rule/tooldef files belong to what (#1141).

Every bundled rules file (generic ``rules/*`` or a host layer such as AgentWire's) and
every ``tooldefs/*`` belongs to exactly one entry here (entries name the authored ``.yaml``;
the JSON a hook reads shares its stem). File names must be unique across the rules dirs.
``kind="group"`` entries are default-on ("os-focused"); a human may opt one out. ``kernel`` groups can never be opted out. ``kind="pack"`` entries are vendor/tool
sets a human enables (``skipped_stems`` says what an undecided policy loads: no packs but the host's defaults).

The generic registry lives here; what the HOST adds (its own groups, its layer files
attached to generic groups, the packs it turns on) comes from the installed profile
(#1209 M3), so the registry has no host names in its logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from damage_control import engine
from damage_control.profile import GroupSpec as Entry

_GENERIC_ENTRIES = (
    # --- default groups -----------------------------------------------------
    Entry("control-plane", "group", "Protected damage-control/agent config paths", ("control-plane.yaml",), kernel=True),
    Entry("core", "group", "Filesystem, process and history destruction", ("core.yaml",), kernel=True),
    Entry("git", "group", "Destructive git operations, gh repo delete + git/gh tooldefs", ("git.yaml",), ("git.yaml", "gh.yaml")),
    Entry("uv", "group", "uv run/publish ask-tier (own group: opting out of git must not drop it)", (), ("uv.yaml",)),
    Entry("registry", "group", "npm unpublish + npm publish ask-tier", ("registry.yaml",), ("npm.yaml",)),
    Entry("payloads", "group", "Rules scanning payloads wrapped in other commands", ("payloads.yaml",)),
    Entry("remote", "group", "ssh-only remote hazards (reboot, service stop)", ("remote.yaml",)),
    Entry("mcp-tools", "group", "Per-tool policy for MCP tool calls", ("mcp-tools.yaml",)),
    Entry("outbound", "group", "Outward-facing sends (email, SMS, push)", ("outbound.yaml",)),
    # --- packs --------------------------------------------------------------
    Entry("aws", "pack", "AWS CLI", ("aws.yaml",), ("aws.yaml",), binaries=("aws",)),
    Entry("gcp", "pack", "gcloud", ("gcp.yaml",), ("gcp.yaml",), binaries=("gcloud",)),
    Entry("firebase", "pack", "Firebase CLI", ("firebase.yaml",), binaries=("firebase",)),
    Entry("cloud-hosting", "pack", "Vercel, Netlify, Cloudflare, Heroku, Fly, DO, Supabase", ("cloud-hosting.yaml",), binaries=("vercel", "netlify", "wrangler", "heroku", "flyctl", "doctl", "supabase")),
    Entry("databases", "pack", "Database CLIs and SQL", ("databases.yaml",), binaries=("psql", "mysql", "mongosh", "redis-cli", "prisma", "rails", "alembic", "flyway", "knex", "sequelize")),
    Entry("containers", "pack", "Docker and Kubernetes", ("containers.yaml",), ("docker.yaml", "kubectl.yaml"), binaries=("docker", "kubectl")),
    Entry("infrastructure", "pack", "Terraform, Pulumi, Serverless, SAM", ("infrastructure.yaml",), ("terraform.yaml",), binaries=("terraform", "pulumi", "serverless", "sam")),
    Entry("tmux", "pack", "tmux kill-server / kill-session -a", ("tmux.yaml",), binaries=("tmux",)),
    Entry("publish", "pack", "cargo/poetry/twine/pnpm/yarn/gem/mvn publish", ("publish.yaml",), binaries=("cargo", "poetry", "twine", "pnpm", "yarn", "gem", "mvn")),
)


def _with_profile(generic: Tuple[Entry, ...], profile) -> Tuple[Entry, ...]:
    """``generic`` plus what ``profile`` adds: layer files on generic groups, then its own groups."""
    layers = dict(profile.group_layer_rules)
    unknown = sorted(set(layers) - {e.name for e in generic})
    if unknown:
        raise ValueError(f"profile {profile.name!r} adds rule files to unknown groups: {unknown}")
    attached = tuple(e._replace(layer_rules=e.layer_rules + layers.get(e.name, ())) for e in generic)
    return attached + tuple(profile.groups)


def entries() -> Tuple[Entry, ...]:
    """Every group and pack of THIS process: the generic registry plus what its profile adds."""
    return _with_profile(_GENERIC_ENTRIES, engine.PROFILE)


def registry() -> Dict[str, Entry]:
    return {e.name: e for e in entries()}


def default_packs() -> Tuple[str, ...]:
    """Packs the host turns on for itself."""
    return tuple(engine.PROFILE.default_packs)


def skipped_stems(packs: Optional[List[str]], disabled_groups: List[str]) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """``(rules stems, tooldef stems)`` of the bundled files switched off by the policy.

    ``packs`` None means the policy has no ``packs:`` key (undecided; an unreadable policy never gets here, the loader supplies every pack): the
    default groups are on and only the HOST's default packs load (none for the generic profile),
    never "everything off" (#1209 M5, section 4.5). A machine that has decided lists its packs.
    A kernel group is never skipped.
    """
    if packs is None:
        packs = list(default_packs())
    rules: set = set()
    tooldefs: set = set()
    for e in entries():
        if e.kind == "pack":
            active = e.name in packs
        else:
            active = e.kernel or e.name not in disabled_groups
        if not active:
            rules.update(Path(f).stem for f in e.files("rules"))
            tooldefs.update(Path(f).stem for f in e.files("tooldefs"))
    return frozenset(rules), frozenset(tooldefs)


def bundled_files() -> Dict[str, set]:
    """The ``{"rules": {...}, "tooldefs": {...}}`` file stems actually bundled (generic + profile layers)."""
    return {
        "rules": {p.stem for d in engine.bundled_rule_sources() for p in engine._data_files(d)},
        "tooldefs": {p.stem for p in engine._data_files(engine.bundled_tooldefs_dir())},
    }


def unclassified() -> Dict[str, list]:
    """Bundled files owned by no entry, or by more than one, per section."""
    bundled = bundled_files()
    out: Dict[str, list] = {}
    for section in ("rules", "tooldefs"):
        owners: Dict[str, list] = {}
        for e in entries():
            for f in e.files(section):
                owners.setdefault(Path(f).stem, []).append(e.name)
        bad = [f"{f}: owned by {owners.get(f, [])}" for f in sorted(bundled[section] | set(owners))
               if f not in bundled[section] or len(owners.get(f, [])) != 1]
        if bad:
            out[section] = bad
    return out
