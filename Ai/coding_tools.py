import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from queue import Empty, Queue
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from . import phone_model
except ImportError:
    import phone_model


PROMPT_PLACEHOLDER = "{prompt}"
MAX_PLAN_LENGTH = 20_000
MAX_AGENT_OUTPUT = 200_000
MAX_AGENT_PATCH = 5_000_000
MAX_CODING_HISTORY_TURNS = 40
MAX_CODING_HISTORY_CHARS = 20_000

BUILTIN_SKILLS = {
    "python": (
        "Python skill: use clear, idiomatic Python; preserve type safety; handle "
        "errors explicitly; add focused tests; avoid broad exception handling; "
        "validate with the project's configured test and lint commands."
    ),
    "lua": (
        "Lua skill: use idiomatic Lua, keep APIs and runtime compatibility in "
        "mind, avoid global state, validate input at boundaries, and test the "
        "affected behavior."
    ),
    "roblox-luau": (
        "Roblox Luau skill: use strict Luau typing where appropriate, respect "
        "client/server trust boundaries, validate RemoteEvent and RemoteFunction "
        "input on the server, avoid yielding in unsafe state transitions, and "
        "preserve Roblox service and lifecycle conventions."
    ),
}

PROVIDER_SPECS = {
    "copilot": {
        "label": "GitHub Copilot CLI",
        "executable": "copilot",
        "template": "copilot -p {prompt}",
        "website": "https://docs.github.com/en/copilot",
    },
    "claude": {
        "label": "Claude Code",
        "executable": "claude",
        "template": "claude -p {prompt}",
        "website": "https://docs.anthropic.com/en/docs/claude-code",
    },
    "codex": {
        "label": "OpenAI Codex CLI",
        "executable": "codex",
        "template": "codex exec {prompt}",
        "website": "https://github.com/openai/codex",
    },
    "aider": {
        "label": "Aider",
        "executable": "aider",
        "template": "aider --message {prompt}",
        "website": "https://aider.chat/",
    },
    "opencode": {
        "label": "OpenCode",
        "executable": "opencode",
        "template": "",
        "website": "https://opencode.ai/",
    },
    "gemini": {
        "label": "Gemini CLI",
        "executable": "gemini",
        "template": "",
        "website": "https://github.com/google-gemini/gemini-cli",
    },
    "cursor-agent": {
        "label": "Cursor Agent CLI",
        "executable": "cursor-agent",
        "template": "",
        "website": "https://docs.cursor.com/",
    },
    "amp": {
        "label": "Amp CLI",
        "executable": "amp",
        "template": "",
        "website": "https://ampcode.com/",
    },
    "custom": {
        "label": "Custom coding CLI",
        "executable": "",
        "template": "",
        "website": "",
    },
}

# Fixed, vetted install commands. Anything not listed here (for example Cursor
# Agent, whose official installer pipes a script into a shell) stays manual.
INSTALL_STEPS: dict[str, list[list[str]]] = {
    "copilot": [["npm", "install", "-g", "@github/copilot"]],
    "claude": [["npm", "install", "-g", "@anthropic-ai/claude-code"]],
    "codex": [["npm", "install", "-g", "@openai/codex"]],
    "gemini": [["npm", "install", "-g", "@google/gemini-cli"]],
    "opencode": [["npm", "install", "-g", "opencode-ai"]],
    "amp": [["npm", "install", "-g", "@sourcegraph/amp"]],
    "aider": [["pipx", "install", "aider-chat"]],
}
PREREQUISITE_INSTALLERS: dict[str, dict[str, list[str]]] = {
    "npm": {"brew": ["brew", "install", "node"], "pkg": ["pkg", "install", "-y", "nodejs"]},
    "pipx": {"brew": ["brew", "install", "pipx"]},
}
INSTALL_TIMEOUT_SECONDS = 900


@dataclass
class CodingState:
    provider: str = "copilot"
    command_template: str = "copilot -p {prompt}"
    coding_mode: str = "brief"
    workspace: str = ""
    skills: list[str] = field(default_factory=list)
    custom_skills: dict[str, str] = field(default_factory=dict)
    coding_history: list[dict[str, str]] = field(default_factory=list)
    plan_request: str = ""
    plan: str = ""
    plan_workspace: str = ""
    plan_status: str = "idle"
    job_status: str = "idle"
    job_output: str = ""
    job_command: str = ""
    job_patch: str = ""
    command_cwd: str = ""
    command_request: str = ""
    proposed_command: str = ""
    command_output: str = ""


def load_coding_state(path: Path) -> CodingState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CodingState()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read coding state at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Coding state at {path} must contain a JSON object")
    defaults = asdict(CodingState())
    values: dict[str, Any] = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if key == "coding_mode":
            values[key] = (
                value
                if isinstance(value, str)
                and value in {"brief", "confirm", "auto"}
                else default
            )
        elif key == "skills":
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                values[key] = value
            else:
                values[key] = default
        elif key == "custom_skills":
            if (
                isinstance(value, dict)
                and all(
                    isinstance(name, str) and isinstance(text, str)
                    for name, text in value.items()
                )
            ):
                values[key] = value
            else:
                values[key] = default
        elif key == "coding_history":
            if isinstance(value, list) and all(
                isinstance(turn, dict)
                and isinstance(turn.get("role"), str)
                and turn.get("role") in {"user", "assistant", "agent"}
                and isinstance(turn.get("content"), str)
                for turn in value
            ):
                values[key] = value[-MAX_CODING_HISTORY_TURNS:]
            else:
                values[key] = default
        elif isinstance(value, type(default)):
            values[key] = value
    return CodingState(**values)


def save_coding_state(path: Path, state: CodingState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def discover_providers() -> list[dict[str, str | bool]]:
    providers: list[dict[str, str | bool]] = []
    for provider_id, spec in PROVIDER_SPECS.items():
        if not spec["executable"]:
            continue
        executable = shutil.which(str(spec["executable"]))
        providers.append(
            {
                "id": provider_id,
                "label": str(spec["label"]),
                "executable": executable or str(spec["executable"]),
                "available": executable is not None,
                "website": str(spec["website"]),
            }
        )
    return providers


def install_plan(provider_id: str) -> list[list[str]]:
    spec = PROVIDER_SPECS.get(provider_id)
    if spec is None:
        raise ValueError(f"Unknown coding agent: {provider_id}")
    steps = INSTALL_STEPS.get(provider_id)
    if not steps:
        raise ValueError(
            f"{spec['label']} has no automatic installer; install it manually."
        )
    plan: list[list[str]] = []
    prepared: set[str] = set()
    for step in steps:
        tool = step[0]
        if shutil.which(tool) is None and tool not in prepared:
            installer = next(
                (
                    command
                    for manager, command in PREREQUISITE_INSTALLERS.get(tool, {}).items()
                    if shutil.which(manager)
                ),
                None,
            )
            if installer is None:
                raise ValueError(
                    f"'{tool}' is required but is not installed and cannot be "
                    "installed automatically; install it, then retry."
                )
            plan.append(installer)
            prepared.add(tool)
        plan.append(step)
    return plan


def run_install(steps: list[list[str]]) -> tuple[int, str]:
    log: list[str] = []
    for argv in steps:
        resolved = shutil.which(argv[0])
        if resolved is None:
            log.append(f"'{argv[0]}' was not found on PATH.")
            return 127, "\n".join(log)[-MAX_AGENT_OUTPUT:]
        log.append(f"$ {shlex.join(argv)}")
        try:
            result = subprocess.run(
                [resolved, *argv[1:]],
                cwd=Path.home(),
                env=command_environment(),
                capture_output=True,
                text=True,
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Installation exceeded the {INSTALL_TIMEOUT_SECONDS // 60}-minute limit."
            ) from exc
        log.append(f"{result.stdout}{result.stderr}".strip())
        if result.returncode:
            return result.returncode, "\n".join(log)[-MAX_AGENT_OUTPUT:]
    return 0, "\n".join(log)[-MAX_AGENT_OUTPUT:]


def provider_template(provider_id: str) -> str:
    spec = PROVIDER_SPECS.get(provider_id)
    if spec is None:
        raise ValueError(f"Unknown coding agent: {provider_id}")
    if not spec["template"]:
        raise ValueError(f"Configure the native command for {spec['label']} first.")
    return str(spec["template"])


def build_agent_argv(command_template: str, prompt: str) -> list[str]:
    if PROMPT_PLACEHOLDER not in command_template:
        raise ValueError(f"Agent command must include {PROMPT_PLACEHOLDER}")
    try:
        template_args = shlex.split(command_template)
    except ValueError as exc:
        raise ValueError(f"Invalid agent command template: {exc}") from exc
    if not template_args:
        raise ValueError("Agent command cannot be empty")
    args = [argument.replace(PROMPT_PLACEHOLDER, prompt) for argument in template_args]
    if not args[0] or args[0] == PROMPT_PLACEHOLDER:
        raise ValueError("Agent command must start with an executable")
    return args


def discover_skills(
    workspace: Path,
    custom_skills: dict[str, str] | None = None,
) -> dict[str, str]:
    skills = dict(BUILTIN_SKILLS)
    skill_dirs = [
        workspace / ".github" / "skills",
        workspace / ".claude" / "skills",
        workspace / "Ai" / "skills",
        Path.home() / ".copilot" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    for skill_dir in skill_dirs:
        if not skill_dir.is_dir():
            continue
        try:
            files = sorted(skill_dir.glob("*/SKILL.md"))
        except OSError:
            continue
        for skill_file in files:
            try:
                contents = skill_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if contents:
                skills.setdefault(skill_file.parent.name, contents[:20_000])
    for name, instructions in (custom_skills or {}).items():
        skills[name] = instructions
    return skills


def selected_skill_text(skill_names: list[str], skills: dict[str, str]) -> str:
    missing = [name for name in skill_names if name not in skills]
    if missing:
        raise ValueError(f"Unknown skill(s): {', '.join(missing)}")
    if not skill_names:
        return "No additional skills selected."
    return "\n\n".join(f"## {name}\n{skills[name]}" for name in skill_names)


def coding_history_text(history: list[dict[str, str]]) -> str:
    text = "\n\n".join(
        f"{turn['role'].upper()}:\n{turn['content']}"
        for turn in history[-MAX_CODING_HISTORY_TURNS:]
    )
    return text[-MAX_CODING_HISTORY_CHARS:] or "(No prior coding-mode discussion.)"


def ask_phone_model(
    instruction: str,
    prompt: str,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> str:
    return phone_model.chat(
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": prompt},
        ],
        response_schema=response_schema,
        model=model,
    )


def make_plan(
    workspace: Path,
    request: str,
    coding_history: list[dict[str, str]],
    skill_text: str,
    model: str | None = None,
    workspace_context: str = "",
    chat_context: str = "",
) -> str:
    return ask_phone_model(
        (
            "You are the planning assistant for a local coding agent. Turn the "
            "user's request and coding-mode discussion into a precise, bounded "
            "implementation brief. State goals, likely files/surfaces, constraints, "
            "and validation. Resolve references using the supplied normal-chat "
            "context and coding-mode history, but include only implementation-"
            "relevant facts in the brief. Do not claim to have changed files. "
            "Do not propose shell commands for execution. Ask concise "
            "clarification questions only when a critical requirement is "
            "genuinely ambiguous."
        ),
        (
            f"Workspace: {workspace}\n"
            f"User coding request: {request}\n\n"
            "Coding-mode discussion history:\n"
            f"{coding_history_text(coding_history)}\n\n"
            f"Recent normal-chat context:\n{chat_context[-12_000:] or '(None)'}\n\n"
            f"Workspace context:\n{workspace_context}\n\n"
            f"Selected skills:\n{skill_text}"
        ),
        model,
    )


def detect_coding_request(
    workspace: Path,
    request: str,
    skill_text: str,
    model: str | None = None,
    chat_context: str = "",
    coding_history: list[dict[str, str]] | None = None,
) -> bool:
    result = ask_phone_model(
        (
            "Classify whether the user's message asks to create, modify, debug, "
            "review, test, explain implementation details of, or plan software or "
            "code in a project. Use the supplied chat and coding-mode context to "
            "resolve references such as 'that project' or 'the fix we discussed'. "
            "Greetings, ordinary chat, and coding-related small talk are not "
            "coding requests. Reply with only YES or NO."
        ),
        (
            f"Workspace: {workspace}\n"
            f"Recent normal-chat context:\n{chat_context[-12_000:] or '(None)'}\n\n"
            f"Coding-mode discussion:\n"
            f"{coding_history_text(coding_history or [])}\n\n"
            f"Current user message:\n{request}\n\n"
            f"Coding skills:\n{skill_text}"
        ),
        model,
    )
    answer = result.strip().upper()
    if answer not in {"YES", "NO"}:
        raise ValueError("Phone model returned an invalid coding-request classification.")
    return answer == "YES"


def refine_plan(
    workspace: Path,
    plan: str,
    feedback: str,
    model: str | None = None,
    coding_history: list[dict[str, str]] | None = None,
) -> str:
    return ask_phone_model(
        (
            "You are helping the user refine a coding-agent brief. Discuss the "
            "brief and return an updated, complete brief. Do not execute commands "
            "or claim files were changed. Use coding-mode discussion history, "
            "not normal chat history."
        ),
        (
            f"Workspace: {workspace}\n"
            f"Current brief:\n{plan}\n\n"
            f"Coding-mode discussion history:\n"
            f"{coding_history_text(coding_history or [])}\n\n"
            f"User feedback:\n{feedback}"
        ),
        model,
    )


def suggest_command(
    workspace: Path,
    request: str,
    model: str | None = None,
) -> str:
    result = ask_phone_model(
        (
            "Produce exactly one command as a single executable followed by "
            "arguments, with no shell pipes, redirects, chaining, or expansion. "
            "Return only JSON with one string field named "
            '"command". Do not use a shell command to fetch or execute remote '
            "scripts. Prefer non-destructive commands."
        ),
        f"Workspace: {workspace}\nUser request: {request}",
        model,
        response_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    result = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.strip())
    try:
        command_data = json.loads(result)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Phone model did not return valid command JSON: {exc}") from exc
    command = command_data.get("command") if isinstance(command_data, dict) else None
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Phone model's response did not contain a command.")
    return command.strip()


def command_argv(command: str) -> list[str]:
    if "\x00" in command:
        raise ValueError("Commands cannot contain null characters.")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid command syntax: {exc}") from exc
    if not argv:
        raise ValueError("Command cannot be empty.")
    return argv


def command_environment(cwd: Path | None = None) -> dict[str, str]:
    sensitive_name = re.compile(
        r"(?:API_KEY|_KEY$|TOKEN|SECRET|PASSWORD|CREDENTIAL|GEMINI|GOOGLE_API_KEY)",
        re.IGNORECASE,
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not sensitive_name.search(key.upper())
        and key not in {"OLDPWD", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    if cwd is not None:
        environment["PWD"] = str(cwd)
    return environment


def run_command(argv: list[str], workspace: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            argv,
            cwd=workspace,
            env=command_environment(workspace),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Approved command exceeded the 10-minute time limit.") from exc
    output = f"{result.stdout}{result.stderr}"
    return result.returncode, output[-MAX_AGENT_OUTPUT:]


def run_agent(
    command_template: str,
    prompt: str,
    workspace: Path,
) -> tuple[int, str, list[str]]:
    argv = build_agent_argv(command_template, prompt)
    result = subprocess.run(
        argv,
        cwd=workspace,
        env=command_environment(workspace),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    output = f"{result.stdout}{result.stderr}"[-MAX_AGENT_OUTPUT:]
    return result.returncode, output, argv


class AgentProcess:
    def __init__(self, command_template: str, prompt: str, workspace: Path) -> None:
        self.argv = build_agent_argv(command_template, prompt)
        self.sandbox = Path(tempfile.mkdtemp(prefix="super-bot-agent-")) / "workspace"
        try:
            shutil.copytree(
                workspace,
                self.sandbox,
                ignore=self._copy_ignore,
                symlinks=False,
            )
            self._initialize_git_snapshot()
            self.process = subprocess.Popen(
                self.argv,
                cwd=self.sandbox,
                env=command_environment(self.sandbox),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            shutil.rmtree(self.sandbox.parent, ignore_errors=True)
            raise
        self.output: list[str] = []
        self.lines: Queue[str] = Queue()
        self.cancelled = False
        self.patch = ""
        self.patch_error = ""
        self.return_code: int | None = None
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()

    @staticmethod
    def _copy_ignore(directory: str, names: list[str]) -> set[str]:
        ignored_names = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }
        ignored = set()
        for name in names:
            source = Path(directory) / name
            if name in ignored_names or name == ".env" or name.startswith(".env."):
                ignored.add(name)
            elif source.is_symlink():
                ignored.add(name)
            elif source.suffix.lower() in {".pem", ".p12", ".pfx"}:
                ignored.add(name)
            elif name.lower().startswith(("id_rsa", "id_ed25519")):
                ignored.add(name)
            elif name.lower() in {"secrets.json", "credentials.json", "token.json"}:
                ignored.add(name)
        return ignored

    def _initialize_git_snapshot(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.sandbox,
            check=True,
            timeout=30,
            env=command_environment(self.sandbox),
        )
        subprocess.run(
            ["git", "add", "--force", "--all"],
            cwd=self.sandbox,
            check=True,
            timeout=60,
            env=command_environment(self.sandbox),
        )
        tree = subprocess.run(
            ["git", "write-tree"],
            cwd=self.sandbox,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=command_environment(self.sandbox),
        )
        commit = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Super Bot Local Snapshot",
                "-c",
                "user.email=local-snapshot@invalid",
                "commit-tree",
                tree.stdout.strip(),
                "-m",
                "Local agent baseline",
            ],
            cwd=self.sandbox,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=command_environment(self.sandbox),
        )
        subprocess.run(
            ["git", "update-ref", "HEAD", commit.stdout.strip()],
            cwd=self.sandbox,
            check=True,
            timeout=30,
            env=command_environment(self.sandbox),
        )

    def _read_output(self) -> None:
        if self.process.stdout is not None:
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\r\n"))
            self.process.stdout.close()
        self.return_code = self.process.wait()
        try:
            subprocess.run(
                ["git", "add", "--force", "--all"],
                cwd=self.sandbox,
                check=True,
                timeout=60,
                env=command_environment(self.sandbox),
            )
            result = subprocess.run(
                ["git", "diff", "--cached", "HEAD", "--binary"],
                cwd=self.sandbox,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=command_environment(self.sandbox),
                check=True,
            )
            if len(result.stdout) > MAX_AGENT_PATCH:
                self.patch_error = (
                    f"Agent diff exceeds the {MAX_AGENT_PATCH}-character review limit."
                )
            else:
                self.patch = result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            self.patch_error = f"Could not prepare the agent diff: {exc}"
        finally:
            shutil.rmtree(self.sandbox.parent, ignore_errors=True)

    def collect_output(self) -> list[str]:
        collected = []
        while True:
            try:
                line = self.lines.get_nowait()
            except Empty:
                break
            collected.append(line)
            self.output.append(line)
        if self.output:
            combined = "\n".join(self.output)
            if len(combined) > MAX_AGENT_OUTPUT:
                self.output = [combined[-MAX_AGENT_OUTPUT:]]
        return collected

    @property
    def returncode(self) -> int | None:
        return self.return_code

    def cancel(self) -> None:
        self.cancelled = True
        if self.process.poll() is None:
            self.process.terminate()


def provider_help(executable: str, workspace: Path) -> tuple[int, str]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(f"CLI '{executable}' is not installed or not on PATH.")
    try:
        result = subprocess.run(
            [resolved, "--help"],
            cwd=workspace,
            env=command_environment(workspace),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The coding CLI did not return its help within 15 seconds.") from exc
    return result.returncode, f"{result.stdout}{result.stderr}"[-MAX_AGENT_OUTPUT:]


def git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--"],
        cwd=workspace,
        env=command_environment(workspace),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not read git diff.")
    return result.stdout or "No tracked-file diff is currently available."


def apply_agent_patch(workspace: Path, patch: str) -> None:
    if not patch:
        raise ValueError("There are no agent changes to apply.")
    for arguments in (
        ["git", "apply", "--check", "--binary"],
        ["git", "apply", "--binary"],
    ):
        result = subprocess.run(
            arguments,
            cwd=workspace,
            input=patch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=command_environment(workspace),
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"Could not apply the agent diff safely: {detail or 'git apply failed'}"
            )
