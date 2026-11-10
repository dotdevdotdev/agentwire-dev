"""Plain-language, deterministic summary of a permission ask (#1343).

Built ONLY from the PermissionRequest hook payload (``tool_name``,
``tool_input``, ``agent_type``, ``agent_id``) plus the session name. It depends
on no guard, reads no reason text, and does no I/O, so the spoken alert is
always available and never blocks the permission wait.

The output is one whole sentence phrased as a question. Every action phrase is
fixed text, so no path, flag or raw command text can be read aloud.
"""

import re
import shlex

_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&\n]")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SKIP_WORDS = {"sudo", "time", "env", "nohup", "command", "exec"}

# (verb phrase for a single use, noun-ish phrase joined after "to" when several)
_COMMAND_ACTIONS = {
    "mkdir": "create folders",
    "rm": "delete files",
    "rmdir": "delete folders",
    "mv": "move files",
    "cp": "copy files",
    "touch": "create files",
    "chmod": "change file permissions",
    "chown": "change file ownership",
    "ln": "create links",
    "curl": "make a web request",
    "wget": "download from the internet",
    "ssh": "connect to another machine",
    "scp": "copy files to another machine",
    "rsync": "sync files",
    "gh": "use GitHub",
    "pytest": "run the tests",
    "python": "run a script",
    "python3": "run a script",
    "node": "run a script",
    "bash": "run a script",
    "sh": "run a script",
    "uv": "manage Python packages",
    "pip": "install Python packages",
    "pip3": "install Python packages",
    "npm": "run an npm command",
    "npx": "run an npm tool",
    "pnpm": "run an npm command",
    "yarn": "run an npm command",
    "brew": "manage installed software",
    "docker": "run a container command",
    "make": "build the project",
    "tmux": "control terminal sessions",
    "agentwire": "control agentwire sessions",
    "kill": "stop a process",
    "pkill": "stop a process",
    "sed": "edit text",
    "awk": "process text",
    "tar": "pack or unpack files",
    "unzip": "unpack files",
    "open": "open something on the computer",
    "code": "open an editor",
}
_READ_ONLY = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "sort", "diff",
    "stat", "file", "du", "df", "pwd", "echo", "printf", "which", "tree",
    "less", "jq", "test", "true", "date", "env",
}
_GIT_ACTIONS = {
    "push": "push code to GitHub",
    "commit": "commit changes",
    "pull": "pull code from GitHub",
    "fetch": "fetch code from GitHub",
    "merge": "merge branches",
    "rebase": "rebase a branch",
    "reset": "reset the repository",
    "checkout": "switch branches",
    "switch": "switch branches",
    "branch": "manage branches",
    "worktree": "manage worktrees",
    "stash": "stash changes",
    "clone": "clone a repository",
    "add": "stage changes",
    "tag": "tag a release",
    "clean": "clean untracked files",
}
_PACKAGE_INSTALL = {"install", "add", "i", "sync"}

_FILE_TOOLS = {
    "Edit": "edit a file",
    "MultiEdit": "edit a file",
    "Write": "write a file",
    "NotebookEdit": "edit a notebook",
    "Read": "read a file",
    "Glob": "look for files",
    "Grep": "search the code",
    "WebFetch": "fetch a web page",
    "WebSearch": "search the web",
    "Task": "start a helper agent",
    "Agent": "start a helper agent",
    "ExitPlanMode": "get its plan approved",
    "AskUserQuestion": "ask you a question",
}


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies so file contents are never mistaken for commands."""
    out: list[str] = []
    delimiter = None
    for line in command.splitlines():
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m:
            delimiter = m.group(1)
    return "\n".join(out)


def _segment_action(segment: str) -> "str | None":
    """Action phrase for one command segment, ``None`` when read-only/empty."""
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] in _SKIP_WORDS):
        words = words[1:]
    if not words:
        return None
    head = words[0].rsplit("/", 1)[-1]
    rest = [w for w in words[1:] if not w.startswith("-")]
    if head == "cd":
        return None
    if head == "git":
        return _GIT_ACTIONS.get(rest[0], "run a git command") if rest else "run a git command"
    if head in ("uv", "pip", "pip3", "npm", "pnpm", "yarn", "brew") and rest:
        if rest[0] in _PACKAGE_INSTALL:
            return "install packages"
        if rest[0] in ("test", "pytest"):
            return "run the tests"
        if head == "uv" and rest[0] == "run":
            return "run a project command"
    if head in _READ_ONLY:
        return "read files"
    return _COMMAND_ACTIONS.get(head, "run a command")


def _bash_action(command: str) -> str:
    phrases: list[str] = []
    for segment in _SEGMENT_SPLIT.split(_strip_heredocs(command)):
        phrase = _segment_action(segment.strip())
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    # Specific phrases beat the generic fallback when both are present.
    specific = [p for p in phrases if p not in ("run a command", "read files")]
    phrases = specific or phrases
    if not phrases:
        return "run a command"
    if phrases == ["run a command"]:
        return "run a command" if _segment_count(command) <= 1 else "run some commands"
    if phrases == ["read files"]:
        return "run a command to read some files"
    if len(phrases) == 1:
        return f"run a command to {phrases[0]}"
    shown = phrases[:2]
    tail = " and more" if len(phrases) > 2 else ""
    return f"run some commands to {' and '.join(shown)}{tail}"


def _segment_count(command: str) -> int:
    return len([s for s in _SEGMENT_SPLIT.split(_strip_heredocs(command)) if s.strip()])


def _words(name: str) -> str:
    """Letters and digits only, separators spoken as spaces."""
    return " ".join(re.findall(r"[A-Za-z0-9]+", name))


def action_phrase(tool_name: str, tool_input: dict) -> str:
    """What the tool call wants to do, as a verb phrase with no paths."""
    if tool_name == "Bash":
        return _bash_action(str((tool_input or {}).get("command", "")))
    if tool_name in _FILE_TOOLS:
        return _FILE_TOOLS[tool_name]
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = _words(parts[1]) if len(parts) > 1 else ""
        return f"use the {server} integration" if server else "use an integration"
    name = _words(tool_name)
    return f"use the {name} tool" if name else "use a tool"


def _subject(session: str, data: dict) -> str:
    where = _words(session) or "unnamed"
    agent_type = _words(str(data.get("agent_type") or ""))
    if not agent_type:
        return f"The {where} session"
    return f"A {agent_type} in the {where} session"


def summarize(session: str, data: dict) -> str:
    """One whole-sentence spoken question naming the session and the agent."""
    phrase = action_phrase(
        str(data.get("tool_name") or "unknown"), data.get("tool_input") or {}
    )
    return f"{_subject(session, data)} is asking if it can {phrase}."
