"""Rule groups and packs: which bundled rule/tooldef files belong to what (#1141).

Every bundled rules file (generic ``rules/*.yaml`` or the AgentWire layer in
``agentwire-rules/*.yaml``) and every ``tooldefs/*.yaml`` belongs to exactly one
entry here. The two rules dirs are installed FLAT into one live dir, so a file
name must be unique across them. ``kind="group"`` entries are default-on ("os-focused"); a human may
opt one out. ``kernel`` groups can never be opted out. ``kind="pack"`` entries
are vendor/tool sets that stay off until a human enables them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # "group" | "pack"
    description: str
    rules: Tuple[str, ...] = ()
    tooldefs: Tuple[str, ...] = ()
    kernel: bool = False
    binaries: Tuple[str, ...] = ()  # vendor CLIs whose presence suggests a pack
    # AgentWire-owned rule files (``agentwire-rules/``). Owned by the SAME entry as
    # the generic file they were split from, so an opt-out or kernel pin still
    # covers every entry that group covered before the split (#1209 M2).
    agentwire_rules: Tuple[str, ...] = ()

    def files(self, section: str) -> Tuple[str, ...]:
        """Every bundled file this entry owns in ``section`` ("rules" | "tooldefs")."""
        if section == "rules":
            return self.rules + self.agentwire_rules
        return self.tooldefs


_ENTRIES = (
    # --- default groups -----------------------------------------------------
    Entry("control-plane", "group", "Protected damage-control/agent config paths", ("control-plane.yaml",), kernel=True, agentwire_rules=("agentwire-control-plane.yaml",)),
    Entry("core", "group", "Filesystem, process and history destruction", ("core.yaml",), kernel=True, agentwire_rules=("agentwire-core.yaml",)),
    Entry("git", "group", "Destructive git operations, gh repo delete + git/gh tooldefs", ("git.yaml",), ("git.yaml", "gh.yaml")),
    Entry("uv", "group", "uv run/publish ask-tier (own group: opting out of git must not drop it)", (), ("uv.yaml",)),
    Entry("registry", "group", "npm unpublish + npm publish ask-tier", ("registry.yaml",), ("npm.yaml",)),
    Entry("agentwire", "group", "AgentWire state and session destruction", agentwire_rules=("agentwire.yaml",)),
    Entry("payloads", "group", "Rules scanning payloads wrapped in other commands", ("payloads.yaml",)),
    Entry("remote", "group", "ssh-only remote hazards (reboot, service stop)", ("remote.yaml",)),
    Entry("mcp-tools", "group", "Per-tool policy for MCP tool calls", ("mcp-tools.yaml",), agentwire_rules=("agentwire-mcp-tools.yaml",)),
    Entry("outbound", "group", "Outward-facing sends (email, SMS, push)", ("outbound.yaml",), agentwire_rules=("agentwire-outbound.yaml",)),
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

REGISTRY: Dict[str, Entry] = {e.name: e for e in _ENTRIES}

# Packs AgentWire turns on for itself. ``tmux`` was part of the always-on
# ``agentwire`` group before it was carved into a generic opt-in pack (#1209 M2);
# AgentWire is a tmux tool, so its install keeps the two rules by default.
AGENTWIRE_DEFAULT_PACKS: Tuple[str, ...] = ("tmux",)

# Where the bundled rule files live, relative to the package: generic first.
RULE_SOURCE_DIRS: Tuple[Tuple[str, ...], ...] = (
    ("hooks", "damage-control", "rules"),
    ("hooks", "damage-control", "agentwire-rules"),
)


def rule_source_dirs(package_dir: Path) -> list:
    """The bundled rules directories (generic, then the AgentWire layer)."""
    return [package_dir.joinpath(*parts) for parts in RULE_SOURCE_DIRS]


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
    """The bundled rules directories of THIS installed package (generic, then AgentWire layer)."""
    return rule_source_dirs(Path(__file__).resolve().parent.parent)
