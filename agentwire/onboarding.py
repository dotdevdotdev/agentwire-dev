"""Minimal onboarding wizard for AgentWire setup.

Asks 3 questions, writes minimal config, then spawns Claude for interactive setup.
"""

import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".agentwire"

# ANSI colors
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
RESET = "\033[0m"
DIM = "\033[2m"


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}{text}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}\n")


def print_success(text: str) -> None:
    """Print success message."""
    print(f"{GREEN}✓{RESET} {text}")


def print_warning(text: str) -> None:
    """Print warning message."""
    print(f"{YELLOW}!{RESET} {text}")


def print_error(text: str) -> None:
    """Print error message."""
    print(f"{RED}✗{RESET} {text}")


def print_info(text: str) -> None:
    """Print info message."""
    print(f"{DIM}{text}{RESET}")


def prompt(question: str, default: str | None = None) -> str:
    """Prompt user for input with optional default."""
    if default:
        result = input(f"{question} [{default}]: ").strip()
        return result if result else default
    return input(f"{question}: ").strip()


def prompt_choice(question: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """Prompt user to choose from options. Returns the option key."""
    print(question)
    print()
    for i, (key, description) in enumerate(options, 1):
        marker = f"{GREEN}→{RESET}" if i == default else " "
        print(f"  {marker} {i}. {description}")
    print()

    while True:
        choice = input(f"Choose [1-{len(options)}] (default: {default}): ").strip()
        if not choice:
            return options[default - 1][0]
        try:
            idx = int(choice)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print_error(f"Please enter a number between 1 and {len(options)}")


def detect_platform() -> str:
    """Detect the current platform."""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except FileNotFoundError:
            pass
        return "linux"
    return "unknown"


def check_command_exists(cmd: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(cmd) is not None


# ─────────────────────────────────────────────────────────────
# Dependency Checks
# ─────────────────────────────────────────────────────────────


def check_python_version() -> tuple[bool, str]:
    """Check if Python version is >= 3.10."""
    version_info = sys.version_info
    version_string = f"{version_info.major}.{version_info.minor}.{version_info.micro}"
    is_valid = version_info >= (3, 10)
    return is_valid, version_string


def check_tmux() -> tuple[bool, str]:
    """Check if tmux is installed."""
    tmux_path = shutil.which("tmux")
    if tmux_path:
        return True, tmux_path
    return False, "not found"


def check_ffmpeg() -> tuple[bool, str]:
    """Check if ffmpeg is installed."""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return True, ffmpeg_path
    return False, "not found"


def check_claude() -> tuple[bool, str]:
    """Check if Claude Code CLI is installed."""
    claude_path = shutil.which("claude")
    if claude_path:
        return True, claude_path
    return False, "not found"


def check_opencode() -> tuple[bool, str]:
    """Check if OpenCode CLI is installed."""
    opencode_path = shutil.which("opencode")
    if opencode_path:
        return True, opencode_path
    return False, "not found"


def get_install_instructions(platform: str) -> dict[str, str]:
    """Get platform-specific install instructions."""
    if platform == "macos":
        return {
            "tmux": "brew install tmux",
            "ffmpeg": "brew install ffmpeg",
        }
    else:
        return {
            "tmux": "sudo apt install tmux",
            "ffmpeg": "sudo apt install ffmpeg",
        }


# ─────────────────────────────────────────────────────────────
# Main Onboarding
# ─────────────────────────────────────────────────────────────


def run_onboarding(skip_session: bool = False) -> int:
    """Run the minimal onboarding wizard.

    Asks 3 questions:
    1. Projects directory
    2. Agent (Claude Code / OpenCode)
    3. Topology (Standalone / Multi-machine)

    Then writes minimal config and spawns Claude for interactive setup.

    Args:
        skip_session: If True, skip spawning Claude session at the end.
    """
    print()
    print(f"{BOLD}Welcome to AgentWire Setup!{RESET}")
    print()
    print("I'll ask you 3 quick questions, then Claude will help with the rest.")
    print()

    # ─────────────────────────────────────────────────────────────
    # Pre-flight Checks
    # ─────────────────────────────────────────────────────────────
    print_header("Pre-flight Checks")

    platform = detect_platform()
    instructions = get_install_instructions(platform)

    # Check Python
    python_ok, python_version = check_python_version()
    if not python_ok:
        print_error(f"Python {python_version} is too old (required: >=3.10)")
        return 1
    print_success(f"Python {python_version}")

    # Check tmux (required)
    tmux_ok, tmux_path = check_tmux()
    if not tmux_ok:
        print_error(f"tmux not found (required)")
        print_info(f"Install with: {instructions['tmux']}")
        return 1
    print_success(f"tmux: {tmux_path}")

    # Check ffmpeg (optional)
    ffmpeg_ok, ffmpeg_path = check_ffmpeg()
    if not ffmpeg_ok:
        print_warning("ffmpeg not found (voice input won't work)")
        print_info(f"Install with: {instructions['ffmpeg']}")
    else:
        print_success(f"ffmpeg: {ffmpeg_path}")

    # Check agents
    claude_ok, claude_path = check_claude()
    opencode_ok, opencode_path = check_opencode()

    if claude_ok:
        print_success(f"claude: {claude_path}")
    if opencode_ok:
        print_success(f"opencode: {opencode_path}")
    if not claude_ok and not opencode_ok:
        print_warning("No AI agent found (claude or opencode)")
        print_info("Install Claude Code: https://github.com/anthropics/claude-code")
        print_info("Install OpenCode: https://github.com/opencode-ai/opencode")

    # ─────────────────────────────────────────────────────────────
    # Question 1: Projects Directory
    # ─────────────────────────────────────────────────────────────
    print_header("1. Projects Directory")

    print("Where do your code projects live?")
    print_info("Sessions map to subdirectories here (e.g., 'myapp' → ~/projects/myapp/)")
    print()

    projects_dir = prompt("Projects directory", "~/projects")
    projects_path = Path(projects_dir).expanduser()

    if not projects_path.exists():
        print_info(f"Will create {projects_path} when needed")
    else:
        print_success(f"Found {projects_path}")

    # ─────────────────────────────────────────────────────────────
    # Question 2: Agent
    # ─────────────────────────────────────────────────────────────
    print_header("2. AI Agent")

    print("Which AI coding agent should AgentWire use?")
    print()

    agent_options = []
    default_agent = 1

    if claude_ok:
        agent_options.append(("claude", "Claude Code (Anthropic)"))
    if opencode_ok:
        agent_options.append(("opencode", "OpenCode"))

    if not agent_options:
        agent_options.append(("claude", "Claude Code (not installed yet)"))
        agent_options.append(("opencode", "OpenCode (not installed yet)"))

    agent_choice = prompt_choice("", agent_options, default=default_agent)

    if agent_choice == "claude":
        agent_command = "claude --dangerously-skip-permissions"
    else:
        agent_command = "opencode"

    print_success(f"Agent: {agent_command}")

    # ─────────────────────────────────────────────────────────────
    # Question 3: Topology
    # ─────────────────────────────────────────────────────────────
    print_header("3. Setup Type")

    print("How will you use AgentWire?")
    print()

    topology_choice = prompt_choice(
        "",
        [
            ("standalone", "Standalone (single machine, simplest setup)"),
            ("multi", "Multi-machine (portal here, sessions on remote servers)"),
        ],
        default=1,
    )

    is_multi_machine = topology_choice == "multi"
    print_success(f"Setup: {'Multi-machine' if is_multi_machine else 'Standalone'}")

    # ─────────────────────────────────────────────────────────────
    # Write Minimal Config
    # ─────────────────────────────────────────────────────────────
    print_header("Saving Configuration")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Minimal config - Claude will configure the rest
    config_content = f"""# AgentWire Configuration
# Generated by: agentwire init
# Run 'agentwire init' again to reconfigure

server:
  host: "0.0.0.0"
  port: 8765
  ssl:
    cert: "~/.agentwire/cert.pem"
    key: "~/.agentwire/key.pem"

projects:
  dir: "{projects_dir}"
  worktrees:
    enabled: true
    suffix: "-worktrees"

agent:
  command: "{agent_command}"

# TTS/STT will be configured by Claude setup assistant
tts:
  backend: "none"
  url: "http://localhost:8100"
  default_voice: "default"

stt:
  url: ""  # Empty = disabled

# Services configuration
services:
  portal:
    machine: null
    port: 8765
    scheme: "https"
  tts:
    machine: null
    port: 8100
    scheme: "http"
"""

    config_path = CONFIG_DIR / "config.yaml"
    config_path.write_text(config_content)
    print_success(f"Created {config_path}")

    # Empty machines.json
    machines_path = CONFIG_DIR / "machines.json"
    machines_path.write_text('{"machines": []}\n')
    print_success(f"Created {machines_path}")

    # Generate SSL certs if they don't exist
    cert_path = CONFIG_DIR / "cert.pem"
    key_path = CONFIG_DIR / "key.pem"

    if not cert_path.exists() or not key_path.exists():
        print()
        print("Generating SSL certificates...")
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:4096",
                    "-keyout", str(key_path),
                    "-out", str(cert_path),
                    "-days", "365", "-nodes",
                    "-subj", "/CN=localhost",
                ],
                check=True,
                capture_output=True,
            )
            print_success(f"Created {cert_path}")
            print_success(f"Created {key_path}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print_warning("Could not generate SSL certificates")
            print_info("Run 'agentwire generate-certs' later, or Claude will help")

    # ─────────────────────────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────────────────────────
    print_header("Basic Setup Complete!")

    print(f"{BOLD}Your configuration:{RESET}")
    print(f"  Projects:  {projects_dir}")
    print(f"  Agent:     {agent_command}")
    print(f"  Setup:     {'Multi-machine' if is_multi_machine else 'Standalone'}")
    print()

    # ─────────────────────────────────────────────────────────────
    # Spawn Claude for Interactive Setup
    # ─────────────────────────────────────────────────────────────
    if skip_session:
        print(f"{BOLD}Next steps:{RESET}")
        print(f"  1. {CYAN}agentwire portal start{RESET}")
        print(f"  2. Open {CYAN}https://localhost:8765{RESET}")
        print()
        print_info("Run 'agentwire init' again to complete setup with Claude's help.")
        return 0

    print()
    print_info("Now Claude will help you configure TTS, STT, and other services.")
    print_info("This is interactive - Claude will ask questions and test services.")
    print()

    input(f"Press {BOLD}Enter{RESET} to continue with Claude setup...")

    # Spawn Claude session with init role
    print()
    print("Starting Claude setup assistant...")

    try:
        # Create a temporary session for setup
        session_name = "agentwire-init"

        # Build the command
        cmd = [
            "agentwire", "new",
            "-s", session_name,
            "--roles", "init",
            "--type", "claude-bypass",
        ]

        # Run and attach
        subprocess.run(cmd, check=True)
        return 0

    except subprocess.CalledProcessError as e:
        print_error(f"Failed to start Claude session: {e}")
        print()
        print(f"{BOLD}Manual next steps:{RESET}")
        print(f"  1. {CYAN}agentwire portal start{RESET}")
        print(f"  2. {CYAN}agentwire new -s init --roles init{RESET}")
        return 1
    except KeyboardInterrupt:
        print()
        print_info("Setup cancelled. Run 'agentwire init' to continue later.")
        return 0
