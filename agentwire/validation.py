"""
AgentWire configuration validation.

Validates config and machines.json for consistency, providing actionable error messages.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .config import Config, load_config


@dataclass
class ConfigWarning:
    """Non-fatal config issue."""

    message: str
    context: dict = field(default_factory=dict)
    suggestion: str = ""

    def format_message(self) -> str:
        """Format warning for display."""
        lines = [f"WARNING: {self.message}"]
        if self.context:
            lines.append("")
            lines.append("Context:")
            for k, v in self.context.items():
                lines.append(f"  {k}: {v}")
        if self.suggestion:
            lines.append("")
            lines.append(f"Suggestion: {self.suggestion}")
        return "\n".join(lines)


@dataclass
class ConfigError:
    """Fatal config issue that must be fixed."""

    message: str
    context: dict = field(default_factory=dict)
    fix_steps: List[str] = field(default_factory=list)

    def format_message(self) -> str:
        """Format error with WHAT/WHY/HOW."""
        lines = [
            f"ERROR: {self.message}",
            "",
            "Context:",
        ]
        for k, v in self.context.items():
            lines.append(f"  {k}: {v}")
        if self.fix_steps:
            lines.append("")
            lines.append("To fix:")
            for i, step in enumerate(self.fix_steps, 1):
                lines.append(f"  {i}. {step}")
        return "\n".join(lines)


def _load_machines(machines_file: Path) -> tuple[Optional[List[dict]], Optional[str]]:
    """Load machines from JSON file.

    Returns:
        Tuple of (machines_list, error_message)
    """
    if not machines_file.exists():
        return [], None  # Empty is valid, not an error

    try:
        with open(machines_file) as f:
            data = json.load(f)
            return data.get("machines", []), None
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"
    except IOError as e:
        return None, f"Cannot read file: {e}"


def _validate_port(port: int, service_name: str, config_path: str) -> Optional[ConfigError]:
    """Validate that a port is in valid range."""
    if not isinstance(port, int) or port < 1 or port > 65535:
        return ConfigError(
            message=f"Invalid port {port}. Must be between 1 and 65535",
            context={
                "service": service_name,
                "port": port,
                "config_path": config_path,
            },
            fix_steps=[
                f"Edit {config_path}",
                f"Set {service_name} port to a valid value (1-65535)",
            ],
        )
    return None


def _validate_url(url: str, service_name: str, config_path: str) -> List[ConfigWarning]:
    """Validate URL format and return warnings."""
    warnings = []

    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            warnings.append(ConfigWarning(
                message=f"URL missing scheme in {service_name}",
                context={"url": url, "service": service_name},
                suggestion=f"Use full URL like 'http://{url}' or 'https://{url}'",
            ))
        if not parsed.netloc and not parsed.path:
            warnings.append(ConfigWarning(
                message=f"URL appears incomplete in {service_name}",
                context={"url": url, "service": service_name},
                suggestion="Use a complete URL like 'http://hostname:port'",
            ))
    except Exception:
        warnings.append(ConfigWarning(
            message=f"Could not parse URL in {service_name}",
            context={"url": url, "service": service_name},
            suggestion="Check URL format",
        ))

    return warnings


def _get_machine_ids(machines: List[dict]) -> set:
    """Extract machine IDs from machines list."""
    return {m.get("id") for m in machines if m.get("id")}


def validate_config(
    config: Config,
    machines_file: Path,
) -> tuple[List[ConfigWarning], List[ConfigError]]:
    """
    Validate config and return issues.

    Checks:
    - services.*.machine references exist in machines.json
    - ports are valid (1-65535)
    - Referenced files exist
    - URL formats are valid

    Args:
        config: Loaded Config object
        machines_file: Path to machines.json

    Returns:
        Tuple of (warnings, errors)
    """
    warnings: List[ConfigWarning] = []
    errors: List[ConfigError] = []

    config_path = "~/.agentwire/config.yaml"

    # Load machines
    machines, load_error = _load_machines(machines_file)
    if load_error:
        errors.append(ConfigError(
            message=f"Cannot read machines.json: {load_error}",
            context={
                "file": str(machines_file),
            },
            fix_steps=[
                "Run: agentwire init",
                f"Or fix the file manually: {machines_file}",
            ],
        ))
        # Can't validate machine references without valid machines.json
        machines = []

    # Validate server port
    port_error = _validate_port(config.server.port, "server", config_path)
    if port_error:
        errors.append(port_error)

    # Validate TTS URL — only the custom tier has one; the default tier's
    # url is None and must not warn (doctor showed false "URL missing
    # scheme in tts" on every default-tier install).
    if config.tts.url:
        warnings.extend(_validate_url(config.tts.url, "tts", config_path))

    # Validate portal URL
    if config.portal.url:
        warnings.extend(_validate_url(config.portal.url, "portal", config_path))

    # Check if machines.json exists when expected
    if not machines_file.exists():
        warnings.append(ConfigWarning(
            message="No machines.json found",
            context={"expected_path": str(machines_file)},
            suggestion="Run 'agentwire init' to create configuration files",
        ))

    # Validate SSL config
    ssl = config.server.ssl
    if ssl.cert and not ssl.cert.exists():
        warnings.append(ConfigWarning(
            message="SSL certificate file not found",
            context={"path": str(ssl.cert)},
            suggestion="Run 'agentwire generate-certs' to create SSL certificates",
        ))
    if ssl.key and not ssl.key.exists():
        warnings.append(ConfigWarning(
            message="SSL key file not found",
            context={"path": str(ssl.key)},
            suggestion="Run 'agentwire generate-certs' to create SSL certificates",
        ))

    # Validate uploads directory parent exists
    uploads_parent = config.uploads.dir.parent
    if not uploads_parent.exists():
        warnings.append(ConfigWarning(
            message="Uploads directory parent does not exist",
            context={"path": str(config.uploads.dir)},
            suggestion=f"Create the directory: mkdir -p {config.uploads.dir}",
        ))

    # Validate each machine in machines.json has required fields
    if machines:
        for machine in machines:
            machine_id = machine.get("id")
            if not machine_id:
                errors.append(ConfigError(
                    message="Machine entry missing 'id' field",
                    context={
                        "machine": str(machine),
                        "file": str(machines_file),
                    },
                    fix_steps=[
                        f"Edit {machines_file}",
                        "Add 'id' field to the machine entry",
                    ],
                ))
                continue

            if not machine.get("host"):
                errors.append(ConfigError(
                    message=f"Machine '{machine_id}' missing 'host' field",
                    context={
                        "machine_id": machine_id,
                        "file": str(machines_file),
                    },
                    fix_steps=[
                        f"Edit {machines_file}",
                        f"Add 'host' field for machine '{machine_id}'",
                        "Example: \"host\": \"192.168.1.50\" or \"host\": \"my-server.local\"",
                    ],
                ))

    return warnings, errors


def validate_machine_reference(
    machine_id: str,
    service_name: str,
    machines_file: Path,
) -> Optional[ConfigError]:
    """
    Validate that a machine reference exists in machines.json.

    Used for services that reference machines (e.g., TTS on remote machine).

    Args:
        machine_id: The machine ID being referenced
        service_name: Name of the service referencing the machine
        machines_file: Path to machines.json

    Returns:
        ConfigError if machine not found, None if valid
    """
    machines, load_error = _load_machines(machines_file)

    if load_error:
        return ConfigError(
            message=f"Cannot validate machine reference: {load_error}",
            context={
                "machine": machine_id,
                "service": service_name,
                "file": str(machines_file),
            },
            fix_steps=[
                "Run: agentwire init",
                f"Or fix the file manually: {machines_file}",
            ],
        )

    machine_ids = _get_machine_ids(machines) if machines else set()

    if machine_id not in machine_ids:
        # Build helpful error with available machines
        available = ", ".join(sorted(machine_ids)) if machine_ids else "(none registered)"

        return ConfigError(
            message=f"Unknown machine '{machine_id}' in services.{service_name}.machine",
            context={
                "service": service_name,
                "machine": machine_id,
                "config_path": "~/.agentwire/config.yaml",
                "available_machines": available,
            },
            fix_steps=[
                f"Add the machine:    agentwire machine add {machine_id} --host <ip-or-hostname>",
                f"Fix the config:     Edit ~/.agentwire/config.yaml services.{service_name}.machine",
            ],
        )

    return None


def cmd_config_validate(args=None) -> int:
    """Run all config validation checks.

    Args:
        args: Optional argparse namespace (for CLI integration)

    Returns:
        0 if no errors, 1 if errors found
    """
    # Load config
    try:
        config = load_config()
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}")
        print("")
        print("To fix:")
        print("  1. Run: agentwire init")
        print("  2. Or check ~/.agentwire/config.yaml for syntax errors")
        return 1

    # Run validation
    warnings, errors = validate_config(config, config.machines.file)

    # Print results
    if not warnings and not errors:
        print("Config validation passed. No issues found.")
        print("")
        print("Files checked:")
        print("  - ~/.agentwire/config.yaml")
        print(f"  - {config.machines.file}")
        return 0

    # Print warnings first
    if warnings:
        print(f"Found {len(warnings)} warning(s):\n")
        for i, warning in enumerate(warnings, 1):
            print(f"[{i}] {warning.format_message()}")
            print("")

    # Print errors
    if errors:
        print(f"Found {len(errors)} error(s):\n")
        for i, error in enumerate(errors, 1):
            print(f"[{i}] {error.format_message()}")
            print("")
        print("To see registered machines: agentwire machine list")

    # Summary
    print("---")
    print(f"Summary: {len(warnings)} warning(s), {len(errors)} error(s)")

    return 1 if errors else 0
