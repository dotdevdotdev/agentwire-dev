"""Rule groups and packs: which bundled rule/tooldef files belong to what (#1141).

Every bundled rules file (generic ``rules/*.yaml`` or a host layer such as
``agentwire-rules/*.yaml``) and every ``tooldefs/*.yaml`` belongs to exactly one entry
here. The rules dirs are installed FLAT into one live dir, so a file name must be unique
across them. ``kind="group"`` entries are default-on ("os-focused"); a human may opt one
out. ``kernel`` groups can never be opted out. ``kind="pack"`` entries are vendor/tool
sets that stay off until a human enables them.

The generic registry lives here; what the HOST adds (its own groups, its layer files
attached to generic groups, the packs it turns on) comes from the installed profile
(#1209 M3), so the registry has no host names in its logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from agentwire.safety import _core
from agentwire.safety.profile import GroupSpec as Entry

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


_ENTRIES = _with_profile(_GENERIC_ENTRIES, _core.PROFILE)

REGISTRY: Dict[str, Entry] = {e.name: e for e in _ENTRIES}

# Packs the host turns on for itself.
DEFAULT_PACKS: Tuple[str, ...] = tuple(_core.PROFILE.default_packs)


def rule_source_dirs(package_dir: Path) -> list:
    """The bundled rules directories (generic, then the profile's layers)."""
    return _core.bundled_rule_sources(package_dir / "hooks" / "damage-control")


_MERGED: Dict[Path, Path] = {}


def merged_rules_dir(package_dir: Path) -> Path:
    """One flat directory holding every bundled rules file (symlinks to the originals).

    The installed rules dir is flat, and ``load_config`` merges a flat dir in sorted
    file-name order, so a consumer that wants "the bundled rules as an install would
    have them" (heal's sync, doctor's drift, the lint fallback, tests) reads this
    instead of knowing there are two source dirs. Cached per process; removed at exit.
    """
    import atexit
    import shutil
    import tempfile

    cached = _MERGED.get(package_dir)
    if cached is not None and cached.is_dir():
        return cached
    flat = Path(tempfile.mkdtemp(prefix="agentwire-bundled-rules-"))
    atexit.register(shutil.rmtree, flat, True)
    for src_dir in rule_source_dirs(package_dir):
        for src in sorted(src_dir.glob("*.yaml")):
            link = flat / src.name
            if link.exists() or link.is_symlink():
                raise ValueError(f"bundled rule file name {src.name} exists in two source dirs")
            link.symlink_to(src)
    _MERGED[package_dir] = flat
    return flat


def bundled_files(package_dir: Path) -> Dict[str, set]:
    """The ``{"rules": {...}, "tooldefs": {...}}`` file names actually bundled."""
    return {
        "rules": {p.name for d in rule_source_dirs(package_dir) for p in d.glob("*.yaml")},
        "tooldefs": {p.name for p in (package_dir / "tooldefs").glob("*.yaml")},
    }


def unclassified(package_dir: Path) -> Dict[str, list]:
    """Bundled files owned by no entry, or by more than one, per section."""
    bundled = bundled_files(package_dir)
    out: Dict[str, list] = {}
    for section in ("rules", "tooldefs"):
        owners: Dict[str, list] = {}
        for e in _ENTRIES:
            for f in e.files(section):
                owners.setdefault(f, []).append(e.name)
        bad = [f"{f}: owned by {owners.get(f, [])}" for f in sorted(bundled[section] | set(owners))
               if f not in bundled[section] or len(owners.get(f, [])) != 1]
        if bad:
            out[section] = bad
    return out


def bundled_rule_dirs() -> list:
    """The bundled rules directories of THIS installed package (generic, then the profile's layers)."""
    return rule_source_dirs(Path(__file__).resolve().parent.parent)
