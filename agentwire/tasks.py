"""Task configuration and execution for scheduled workloads.

Tasks are defined in .agentwire.yml and executed via `agentwire ensure`.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class TaskError(Exception):
    """Base exception for task-related errors."""

    pass


class TaskNotFound(TaskError):
    """Raised when a task is not found in configuration."""

    pass


class TaskValidationError(TaskError):
    """Raised when task configuration is invalid."""

    pass


class PreCommandError(TaskError):
    """Raised when a pre-command fails."""

    pass


@dataclass
class PreCommand:
    """Configuration for a pre-phase command.

    Pre-commands gather data before the main prompt is sent.
    Their stdout becomes available as {{ var_name }} in templates.
    """

    name: str  # Variable name for output
    cmd: str  # Shell command to run
    required: bool = False  # Fail if output is empty
    validate: str | None = None  # Validation command (receives output via stdin)
    timeout: int = 30  # Command timeout in seconds


@dataclass
class OutputConfig:
    """Configuration for task output handling."""

    capture: int = 50  # Lines to capture from session
    save: str | None = None  # Path to save captured output (supports {{ }})
    notify: str | None = None  # Notification method (voice, alert, webhook ${URL}, command "...")


@dataclass
class TaskConfig:
    """Configuration for a scheduled task.

    Parsed from the `tasks:` section of .agentwire.yml.
    """

    name: str
    prompt: str  # Required: the main prompt to send

    # Shell configuration
    shell: str | None = None  # Override shell for this task

    # Retry configuration
    retries: int = 0  # Number of retries on failure
    retry_delay: int = 30  # Seconds between retries

    # Completion configuration
    idle_timeout: int = 30  # Seconds of idle before completion
    exit_on_complete: bool = True  # Exit session after task completion

    # Loop configuration
    mode: str = "standard"  # "standard" or "loop"
    max_iterations: int = 3  # Safety limit for loop mode (1-20)
    loop_review: bool = True  # Write review file between iterations
    loop_delay: int = 0  # Seconds to wait between loop iterations

    # Pre-phase: data gathering
    pre: list[PreCommand] = field(default_factory=list)

    # Post-completion prompt (after system summary)
    on_task_end: str | None = None

    # Post-phase: commands to run after completion
    post: list[str] = field(default_factory=list)

    # Output handling
    output: OutputConfig = field(default_factory=OutputConfig)

    # Context and branch management
    starting_ref: str | None = None      # Git ref to check out before task runs
    work_branch: str | None = None       # Branch name for agent's work (default: agent/<task>-<date>)
    pr_target: str | None = None         # Branch to PR against (default: starting_ref)
    pr_draft: bool = True                # Create PR as draft
    starting_session: str | None = None  # Fork Claude context from this session before running
    role: str | None = None              # Role override for this task


def parse_pre_command(name: str, config: str | dict) -> PreCommand:
    """Parse a pre-command from config.

    Supports shorthand (string) and expanded (dict) formats.

    Args:
        name: Variable name for the output
        config: Either a string (command) or dict with cmd, required, validate, timeout

    Returns:
        PreCommand instance
    """
    if isinstance(config, str):
        # Shorthand: just the command
        return PreCommand(name=name, cmd=config)

    # Expanded format
    return PreCommand(
        name=name,
        cmd=config.get("cmd", ""),
        required=config.get("required", False),
        validate=config.get("validate"),
        timeout=config.get("timeout", 30),
    )


def parse_output_config(config: dict | None) -> OutputConfig:
    """Parse output configuration from dict.

    Args:
        config: Dict with capture, save, notify keys

    Returns:
        OutputConfig instance
    """
    if not config:
        return OutputConfig()

    return OutputConfig(
        capture=config.get("capture", 50),
        save=config.get("save"),
        notify=config.get("notify"),
    )


def parse_task_config(name: str, config: dict, default_shell: str | None = None) -> TaskConfig:
    """Parse a task configuration from dict.

    Args:
        name: Task name
        config: Task configuration dict
        default_shell: Default shell from project config

    Returns:
        TaskConfig instance

    Raises:
        TaskValidationError: If required fields are missing
    """
    # Required field
    prompt = config.get("prompt")
    if not prompt:
        raise TaskValidationError(f"Task '{name}' is missing required 'prompt' field")

    # Parse pre-commands
    pre_raw = config.get("pre", {})
    pre_commands = []
    if isinstance(pre_raw, dict):
        for var_name, cmd_config in pre_raw.items():
            pre_commands.append(parse_pre_command(var_name, cmd_config))

    # Parse post-commands (list of strings)
    post_raw = config.get("post", [])
    post_commands = post_raw if isinstance(post_raw, list) else [post_raw]

    return TaskConfig(
        name=name,
        prompt=prompt,
        shell=config.get("shell") or default_shell,
        retries=config.get("retries", 0),
        retry_delay=config.get("retry_delay", 30),
        idle_timeout=config.get("idle_timeout", 30),
        exit_on_complete=config.get("exit_on_complete", True),
        mode=config.get("mode", "standard"),
        max_iterations=config.get("max_iterations", 3),
        loop_review=config.get("loop_review", True),
        loop_delay=config.get("loop_delay", 0),
        pre=pre_commands,
        on_task_end=config.get("on_task_end"),
        post=post_commands,
        output=parse_output_config(config.get("output")),
        starting_ref=config.get("starting_ref"),
        work_branch=config.get("work_branch"),
        pr_target=config.get("pr_target"),
        pr_draft=config.get("pr_draft", True),
        starting_session=config.get("starting_session"),
        role=config.get("role"),
    )


def load_task(project_path: Path, task_name: str) -> TaskConfig:
    """Load a task configuration from project's .agentwire.yml.

    Args:
        project_path: Path to project directory
        task_name: Name of the task to load

    Returns:
        TaskConfig instance

    Raises:
        TaskNotFound: If task doesn't exist
        TaskValidationError: If task configuration is invalid
    """
    config_file = project_path / ".agentwire.yml"

    if not config_file.exists():
        raise TaskNotFound(f"No .agentwire.yml found in {project_path}")

    try:
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise TaskValidationError(f"Invalid YAML in .agentwire.yml: {e}")

    tasks = config.get("tasks", {})
    if not tasks:
        raise TaskNotFound(f"No tasks defined in {config_file}")

    if task_name not in tasks:
        available = ", ".join(tasks.keys())
        raise TaskNotFound(f"Task '{task_name}' not found. Available: {available}")

    task_config = tasks[task_name]
    default_shell = config.get("shell")

    return parse_task_config(task_name, task_config, default_shell)


def list_tasks(project_path: Path) -> list[dict[str, Any]]:
    """List all tasks in a project's .agentwire.yml.

    Args:
        project_path: Path to project directory

    Returns:
        List of dicts with task info (name, has_pre, has_post, etc.)
    """
    config_file = project_path / ".agentwire.yml"

    if not config_file.exists():
        return []

    try:
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return []

    tasks = config.get("tasks", {})
    result = []

    for name, task_config in tasks.items():
        if not isinstance(task_config, dict):
            continue

        result.append({
            "name": name,
            "has_pre": bool(task_config.get("pre")),
            "has_post": bool(task_config.get("post")),
            "has_on_task_end": bool(task_config.get("on_task_end")),
            "retries": task_config.get("retries", 0),
            "mode": task_config.get("mode", "standard"),
        })

    return result


def validate_task(task: TaskConfig) -> list[str]:
    """Validate a task configuration.

    Args:
        task: TaskConfig to validate

    Returns:
        List of validation warnings/errors (empty if valid)
    """
    issues = []

    if not task.prompt.strip():
        issues.append("Empty prompt")

    for pre in task.pre:
        if not pre.cmd.strip():
            issues.append(f"Empty command for pre variable '{pre.name}'")
        if pre.timeout <= 0:
            issues.append(f"Invalid timeout for pre variable '{pre.name}'")

    if task.retries < 0:
        issues.append("Negative retry count")

    if task.retry_delay < 0:
        issues.append("Negative retry delay")

    if task.idle_timeout <= 0:
        issues.append("Invalid idle_timeout (must be > 0)")

    if task.mode not in ("standard", "loop"):
        issues.append(f"Invalid mode '{task.mode}' (must be 'standard' or 'loop')")

    if task.max_iterations < 1 or task.max_iterations > 20:
        issues.append(f"Invalid max_iterations {task.max_iterations} (must be 1-20)")

    if task.loop_delay < 0:
        issues.append("Negative loop_delay")

    return issues


def run_pre_command(
    pre: PreCommand,
    shell: str,
    cwd: Path,
) -> str:
    """Run a pre-command and return its output.

    Args:
        pre: PreCommand configuration
        shell: Shell to use (e.g., /bin/sh)
        cwd: Working directory

    Returns:
        Command stdout as string

    Raises:
        PreCommandError: If command fails, times out, or validation fails
    """
    try:
        result = subprocess.run(
            pre.cmd,
            shell=True,
            executable=shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=pre.timeout,
        )
    except subprocess.TimeoutExpired:
        raise PreCommandError(
            f"Pre-command '{pre.name}' timed out after {pre.timeout}s"
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()[:200] if result.stderr else "(no stderr)"
        raise PreCommandError(
            f"Pre-command '{pre.name}' failed with exit code {result.returncode}: {stderr}"
        )

    output = result.stdout.strip()

    # Check required
    if pre.required and not output:
        raise PreCommandError(
            f"Pre-command '{pre.name}' returned empty output (required: true)"
        )

    # Run validation if specified
    if pre.validate and output:
        try:
            val_result = subprocess.run(
                pre.validate,
                shell=True,
                executable=shell,
                cwd=cwd,
                input=output,
                capture_output=True,
                text=True,
                timeout=pre.timeout,
            )
            if val_result.returncode != 0:
                raise PreCommandError(
                    f"Pre-command '{pre.name}' failed validation: {pre.validate}"
                )
        except subprocess.TimeoutExpired:
            raise PreCommandError(
                f"Validation for '{pre.name}' timed out"
            )

    return output


def run_post_command(
    cmd: str,
    shell: str,
    cwd: Path,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Run a post-command.

    Args:
        cmd: Shell command to run
        shell: Shell to use
        cwd: Working directory
        timeout: Command timeout in seconds

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable=shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)
