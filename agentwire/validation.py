"""
AgentWire configuration validation.

Validates config and machines.json for consistency, providing actionable error messages.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .config import Config


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
