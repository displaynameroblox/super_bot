import argparse
import curses
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
import http.client
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import textwrap
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
from queue import Empty, Queue
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    from . import client_data, coding_tools, preflight, tls, transport, tui
    from .protocol import (
        CHAT_SKILLS,
        DISCOVERY_PORT,
        DISCOVERY_REQUEST,
        DISCOVERY_RESPONSE_PREFIX,
        MAX_ACTIVE_CHAT_SKILLS,
        MAX_SKILL_TEXT_LENGTH,
        MAX_STYLE_LENGTH,
        SKILL_NAME,
        normalize_skills,
        STYLE_PRESETS,
    )
except ImportError:
    from Ai import client_data, tui
    import coding_tools
    import preflight
    import tls
    import transport
    from protocol import (
        CHAT_SKILLS,
        DISCOVERY_PORT,
        DISCOVERY_REQUEST,
        DISCOVERY_RESPONSE_PREFIX,
        MAX_ACTIVE_CHAT_SKILLS,
        MAX_SKILL_TEXT_LENGTH,
        MAX_STYLE_LENGTH,
        SKILL_NAME,
        normalize_skills,
        STYLE_PRESETS,
    )


DEFAULT_URL = os.getenv("CHAT_SERVER_URL", "")
REQUEST_TIMEOUT = 500 #too much??
MAX_INITIAL_CHAT_LOG_LENGTH = 10_000
SPEAKER_LINE = re.compile(
    r"^(?:\[(?P<timestamp>[^\]]+)\]\s*)?"
    r"(?P<speaker>[^:\n]{1,100}):(?:\s?(?P<text>.*))?$"
)
ACTION_MARKUP = re.compile(r"(\*\*.*?\*\*|\*.*?\*)")
HELP_TEXT = """\
SUPER BOT HELP

GET STARTED
At launch, enter a session ID to join, or type NEW to create a session.
Type a message and press Enter to send it to the selected conversation.
/help                         Show this guide.
/quit                         Close Super Bot.

SESSIONS AND CONVERSATIONS
/new                          Start a session. Choose a saved profile or enter
                              a player name and persona; session ID and password
                              are optional.
/login [SESSION_ID]            Join a session. If it does not exist, you can
                              create it. Example: /login 23
/connect                      Find a server on your local network or enter an
                              address for a server elsewhere. Connections use
                              HTTPS and end-to-end encryption; a new server's
                              fingerprints are shown once for you to verify.
/server                       Show the verified fingerprints of the current server.
/server forget                Forget them, for example after the server's keys
                              were reset on purpose; /connect verifies it again.
/list                         List locally saved conversations.
/open                         Open the current conversation, or create one if
                              you have no saved sessions.
/open N                       Switch to a saved conversation by its list number.

PROFILES AND CHAT HISTORY
/profile list                 List saved player/persona profiles.
/profile save NAME            Save this conversation's player name, persona,
                              and chat log as a profile.
/profile use NAME             Use a saved profile the next time you create
                              a session.
/profile clear                Stop using the selected profile.
/profile delete NAME          Delete a saved profile.
/setup                        Run the first-run profile setup wizard.
/character status|on NOTES|off
                              Store opt-in local character notes for the selected
                              profile; notes are sent only when creating its chats.
/export [PATH]                Export the active chat to a text file. Without a
                              path, saves beside local chat data.
/style [NAME|TEXT|off]        Set the AI's reply style for this session: a preset
                              (casual, formal, playful, friendly, technical,
                              concise) or your own words. /style lists presets;
                              /style off clears it. Also asked at /new.
/chatskill [list|use NAME|off NAME|clear|show NAME]
                              Chat behaviours (up to 3 per session): roleplay
                              (in-character *actions*), storyteller, copilot-brief
                              (prepare a request for a coding agent), tutor, and
                              your own. Asked at /new; use/off/clear change them
                              mid-session (session host only).
/chatskill create NAME TEXT   Save your own chat skill on this computer (500
                              characters max). The server never stores your
                              skill library; it is sent when you start a session.
/chatskill delete NAME        Delete one of your saved chat skills.
/log load session N           Use a saved session's chat log when creating
                              your next session.
/log load profile NAME        Use a profile's chat log when creating a session.
                              Quote names with spaces, e.g. "Friendly cat".
/log clear                    Clear the staged starting chat log.
/account login|logout|status  Sign in to an account for private knowledge and
                              locally saved chats. Account tokens are memory-only.
/knowledge [list|add FILE [CATEGORY]|delete ID]
                              Manage private account knowledge from the Data tab.

MESSAGES AND PROGRESS
/retry cancel                 Cancel a message retry waiting for its next
                              attempt. After a send failure, confirm to retry;
                              delays increase by 5 seconds each time.
/progress on|off              Show or hide progress for your own messages.
                              The session host can always see everyone's progress.
/learn on|off                 Opt your future messages and AI replies in this
                              session into your local training queue.
                              Training examples are saved only on this client.

CODING WORKFLOW
/code REQUEST                 Ask the phone model to prepare a coding brief.
/refine FEEDBACK              Update the brief with your feedback.
/run                          Send the reviewed brief to your coding CLI.
/coding-mode brief|confirm|auto
                              Choose how coding requests start: brief only,
                              ask before starting, or start automatically.
                              You always review and apply patches yourself.
/status                       Show the current brief and agent status.
/suggest                      Ask the phone model to suggest a bounded coding task.
/coding-only [off]            Show or turn off code-only mode. In this mode,
                              messages go to the coding agent; AI chat,
                              planning, and command suggestions are unavailable.
/workspace [PATH]             Choose the project folder for the AI and coding
                              agents. The selection is saved for next time.

PATCH REVIEW AND COMMANDS
/diff                         Review the agent patch or workspace changes.
/diff workspace               Show workspace changes instead of the agent patch.
/apply                        Apply the reviewed patch after a conflict check.
/discard                      Discard the isolated patch without changing files.
/cancel                       Stop the active coding agent.
In Review, Ctrl+A applies the patch; Ctrl+D discards it.
/command REQUEST              Ask the phone model to suggest a command.
/approve                      Approve and run the suggested command.
/reject                       Reject the suggested command.
AI-proposed commands are never run without your approval.

AGENTS AND SKILLS
/agent list                   List supported coding CLIs and show which are
                              installed.
/agent use NAME               Select copilot, claude, or codex.
/agent configure              Set the CLI command template. Include {prompt}
                              where the coding brief should be inserted.
/agent help                   Show the selected CLI's --help output.
/agent install NAME           Install the CLI after you review and confirm the
                              exact commands (npm/pipx; Cursor Agent is
                              manual). Missing npm/pipx is added via
                              Homebrew or Termux pkg when available.
/skill list                   List built-in, discovered, and custom skills.
/skill use NAME               Add a skill to request detection and agent prompts.
/skill create NAME INSTRUCTIONS
                              Create a skill shared by the AI and coding agents.
/skill edit NAME INSTRUCTIONS Edit one of your custom skills.
/skill delete NAME            Delete one of your custom skills.
/skill show NAME              Show a skill's full instructions.
/skill clear                  Clear selected skills.

VIEWS AND KEYBOARD
/view chat|activity|coding|review|split
                              Switch views. Split shows chat and coding together.
Tab/Shift+Tab                 Switch tabs; F2-F6 jump directly to a tab.
Left/Right, Home/End          Move within your draft (empty draft: navigate views).
Ctrl+U / Ctrl+W               Clear the draft / delete the previous word.
F7                            Toggle split view. F1 shows or hides navigation help.
Ctrl+P                         Search actions and commands; Enter selects.
Ctrl+O                         Find a saved conversation.
Tab after /                    Search commands matching your input.
Up/Down or Page Up/Page Down  Scroll the current view. Home goes to the beginning;
                              End returns to the latest content.

NOTES
Conversations are saved locally after successful server responses. The server
must still have a session in memory for you to join or send messages.
Coding agents can change files in the selected workspace. Review changes before
applying them; Super Bot does not create commits automatically.
"""


class ServerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class TranscriptMessage:
    speaker: str
    timestamp: str | None
    text: str


@dataclass
class ChatSession:
    local_id: str
    player_name: str
    persona: str
    chat_log: str
    server_session_id: int
    access_token: str = ""
    server_url: str = ""
    profile_name: str = ""
    style: str = ""
    chat_skills: dict[str, str] = field(default_factory=dict)
    learn_from_chats: bool = False


@dataclass
class ChatProfile:
    player_name: str
    persona: str
    chat_log: str = ""
    style: str = ""
    chat_skills: dict[str, str] = field(default_factory=dict)
    preferred_model: str = ""
    character_development_enabled: bool = False
    character_notes: str = ""


@dataclass
class ProfileStore:
    profiles: dict[str, ChatProfile]
    selected: str = ""


@dataclass
class UIPreferences:
    show_progress: bool = True
    show_navigation_help: bool = True


@dataclass
class SetupState:
    completed: bool = False
    preferred_model: str = ""


MODEL_CHOICES = (
    "Super Bot Chatter (local adapter)",
    "Existing local phone model",
    "Gemini (server configured)",
)
MAX_CHARACTER_NOTES_LENGTH = 500


@dataclass
class ProgressUpdate:
    participant_id: str
    player_name: str
    phase: str
    detail: str
    updated_at: float


PROGRESS_LABELS = {
    "received": "Received by server",
    "queued": "Queued at server",
    "thinking": "Thinking",
    "gathering_context": "Gathering context",
    "drafting_reply": "Drafting reply",
    "finalizing": "Finalizing reply",
    "complete": "Reply received",
    "failed": "Request failed",
}
TABS = ("Chat", "Data", "Activity", "Coding", "Review")
PROGRESS_ACTIVE_TTL_SECONDS = 60
PROGRESS_TERMINAL_TTL_SECONDS = 5
TERMINAL_PROGRESS_PHASES = {"complete", "failed"}


def default_data_file() -> Path:
    return tls.app_data_dir() / "chat_sessions.json"


def upgrade_legacy_server_url(url: str) -> str:
    """Chats saved before HTTPS was enforced hold http:// addresses of the same server."""
    if url[:7].casefold() == "http://":
        return f"https://{url[7:]}"
    return url


def load_sessions(path: Path) -> list[ChatSession]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read saved chats at {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError(f"Saved chats at {path} must contain a JSON list")

    sessions = []
    required_fields = {
        "local_id": str,
        "player_name": str,
        "persona": str,
        "chat_log": str,
        "server_session_id": int,
    }
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), value_type)
            for key, value_type in required_fields.items()
        ):
            raise ValueError(f"Saved chat #{index} in {path} has an invalid format")
        access_token = item.get("access_token", "")
        server_url = item.get("server_url", "")
        if not isinstance(access_token, str) or not isinstance(server_url, str):
            raise ValueError(f"Saved chat #{index} in {path} has invalid session access data")
        server_url = upgrade_legacy_server_url(server_url)
        chat_skills = item.get("chat_skills", {})
        if not isinstance(chat_skills, dict) or not all(
            isinstance(name, str) and isinstance(text, str)
            for name, text in chat_skills.items()
        ):
            raise ValueError(f"Saved chat #{index} in {path} has invalid chat skills")
        learning_preference = item.get("learn_from_chats", False)
        if not isinstance(learning_preference, bool):
            raise ValueError(
                f"Saved chat #{index} in {path} has invalid learning preference"
            )
        profile_name = item.get("profile_name", "")
        if not isinstance(profile_name, str):
            raise ValueError(f"Saved chat #{index} in {path} has invalid profile name")
        style = item.get("style", "")
        if not isinstance(style, str):
            raise ValueError(f"Saved chat #{index} in {path} has invalid reply style")
        session_data = {
            key: value
            for key, value in item.items()
            if key not in {"access_token", "server_url"}
        }
        sessions.append(
            ChatSession(
                **session_data,
                access_token=access_token,
                server_url=server_url,
            )
        )
    return sessions


def save_sessions(path: Path, sessions: list[ChatSession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps([asdict(session) for session in sessions], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def export_chat(path: Path, chat_log: str) -> None:
    """Write a chat transcript without overwriting an existing export."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("x", encoding="utf-8") as output:
            created = True
            output.write(chat_log)
        path.chmod(0o600)
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise


def load_chat_skills(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read chat skills at {path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(name, str) and isinstance(text, str) for name, text in raw.items()
    ):
        raise ValueError(f"Chat skills at {path} must be a JSON object of text values")
    return raw


def save_chat_skills(path: Path, skills: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(skills, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def response_skills(response: dict[str, Any]) -> dict[str, str]:
    skills = response.get("AI_skills")
    if isinstance(skills, dict) and all(
        isinstance(name, str) and isinstance(text, str) for name, text in skills.items()
    ):
        return dict(skills)
    return {}


def parse_chat_skills(text: str, available: dict[str, str]) -> dict[str, str]:
    """Turn a comma/space separated list of skill names into name -> instructions."""
    names = [name.casefold() for name in re.split(r"[,\s]+", text.strip()) if name]
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(
            f"Unknown chat skill: {', '.join(unknown)}. Available: {', '.join(available)}"
        )
    return normalize_skills({name: available[name] for name in dict.fromkeys(names)})


def load_profiles(path: Path) -> ProfileStore:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ProfileStore(profiles={})
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read profiles at {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise ValueError(f"Saved profiles at {path} have an invalid format")

    profiles: dict[str, ChatProfile] = {}
    for name, profile in raw["profiles"].items():
        if (
            isinstance(name, str)
            and isinstance(profile, dict)
            and isinstance(profile.get("player_name"), str)
            and isinstance(profile.get("persona"), str)
            and (
                "chat_log" not in profile
                or isinstance(profile.get("chat_log"), str)
            )
        ):
            style = profile.get("style", "")
            skills = profile.get("chat_skills", {})
            preferred_model = profile.get("preferred_model", "")
            character_development_enabled = profile.get(
                "character_development_enabled", False
            )
            character_notes = profile.get("character_notes", "")
            if (
                not isinstance(style, str)
                or not isinstance(skills, dict)
                or not all(isinstance(skill, str) and isinstance(text, str) for skill, text in skills.items())
                or not isinstance(preferred_model, str)
                or not isinstance(character_development_enabled, bool)
                or not isinstance(character_notes, str)
                or len(character_notes) > MAX_CHARACTER_NOTES_LENGTH
            ):
                raise ValueError(f"Saved profile {name!r} in {path} has an invalid format")
            profiles[name] = ChatProfile(
                player_name=profile["player_name"],
                persona=profile["persona"],
                chat_log=profile.get("chat_log", ""),
                style=style,
                chat_skills=skills,
                preferred_model=preferred_model,
                character_development_enabled=character_development_enabled,
                character_notes=character_notes,
            )
    selected = raw.get("selected", "")
    if not isinstance(selected, str) or selected not in profiles:
        selected = ""
    return ProfileStore(profiles=profiles, selected=selected)


def save_profiles(path: Path, store: ProfileStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    payload = {
        "selected": store.selected,
        "profiles": {
            name: asdict(profile) for name, profile in store.profiles.items()
        },
    }
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def load_setup_state(path: Path) -> SetupState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SetupState()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read setup state at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Setup state at {path} must contain a JSON object")
    completed = raw.get("completed", False)
    preferred_model = raw.get("preferred_model", "")
    if not isinstance(completed, bool) or not isinstance(preferred_model, str):
        raise ValueError(f"Setup state at {path} has an invalid format")
    return SetupState(completed=completed, preferred_model=preferred_model)


def save_setup_state(path: Path, state: SetupState) -> None:
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


def load_ui_preferences(path: Path) -> UIPreferences:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UIPreferences()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read UI preferences at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"UI preferences at {path} must contain a JSON object")
    return UIPreferences(
        show_progress=(
            raw["show_progress"]
            if isinstance(raw.get("show_progress"), bool)
            else True
        ),
        show_navigation_help=(
            raw["show_navigation_help"]
            if isinstance(raw.get("show_navigation_help"), bool)
            else True
        ),
    )


def save_ui_preferences(path: Path, preferences: UIPreferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(asdict(preferences), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def request_json(
    base_url: str,
    endpoint: str,
    payload: dict[str, object] | None = None,
    access_token: str = "",
) -> dict[str, Any]:
    try:
        status, result = client_data.request(
            base_url,
            "POST" if payload is not None else "GET",
            endpoint,
            payload,
            access_token,
            timeout=REQUEST_TIMEOUT,
        )
    except transport.TransportError as exc:
        raise ServerError(str(exc)) from exc
    if status >= 400:
        detail = result.get("detail") if isinstance(result, dict) else None
        raise ServerError(
            f"Server returned HTTP {status}: "
            f"{detail if detail is not None else http.client.responses.get(status, 'error')}",
            status_code=status,
        )
    if not isinstance(result, dict):
        raise ServerError("Server returned an unexpected response")
    return result


def resolve_style(text: str) -> str:
    """Turn a preset name or free text into the style sent to the server."""
    cleaned = " ".join(text.split())
    if cleaned.casefold() in {"", "off", "none", "default"}:
        return ""
    preset = STYLE_PRESETS.get(cleaned.casefold())
    if preset is not None:
        return preset
    if len(cleaned) > MAX_STYLE_LENGTH:
        raise ValueError(f"Style must be at most {MAX_STYLE_LENGTH} characters")
    return cleaned


def set_session_style(
    base_url: str,
    session_id: int,
    style: str,
    access_token: str,
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/style",
        {"ses_id": session_id, "style": style},
        access_token=access_token,
    )


def set_session_skills(
    base_url: str,
    session_id: int,
    skills: dict[str, str],
    access_token: str,
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/skills",
        {"ses_id": session_id, "skills": skills},
        access_token=access_token,
    )


def start_remote_session(
    base_url: str,
    player_name: str,
    persona: str,
    chat_log: str = "",
    session_id: int | None = None,
    password: str = "",
    style: str = "",
    skills: dict[str, str] | None = None,
    account_token: str = "",
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/start_ses",
        {
            "AI_persona": persona,
            "plr_name": player_name,
            "Chat_log": chat_log,
            "session_ID": session_id,
            "password": password,
            "AI_style": style,
            "AI_skills": skills or {},
        },
        access_token=account_token,
    )


def login_remote_session(
    base_url: str,
    session_id: int,
    password: str,
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/login",
        {"session_ID": session_id, "password": password},
    )


def send_message(
    base_url: str,
    session_id: int,
    message: str,
    access_token: str,
    coding_context: str = "",
    show_progress: bool = True,
    learn_from_chats: bool = False,
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/chat",
        {
            "ses_id": session_id,
            "msg": message,
            "coding_context": coding_context,
            "show_progress": show_progress,
            "learn_from_chats": learn_from_chats,
        },
        access_token=access_token,
    )


def response_session_id(response: dict[str, Any]) -> int:
    session_id = response.get("session_ID")
    if not isinstance(session_id, int):
        raise ServerError("Server response did not include a valid session_ID")
    return session_id


def response_chat_log(response: dict[str, Any]) -> str:
    chat_log = response.get("Chat_log")
    if not isinstance(chat_log, str):
        raise ServerError("Server response did not include a valid Chat_log")
    return chat_log


def response_bool(response: dict[str, Any], field: str) -> bool:
    value = response.get(field)
    if not isinstance(value, bool):
        raise ServerError(f"Server response did not include a valid {field}")
    return value


def response_text(response: dict[str, Any], field: str) -> str:
    value = response.get(field)
    if not isinstance(value, str):
        raise ServerError(f"Server response did not include a valid {field}")
    return value


def discover_servers(timeout: float = 1.5) -> list[str]:
    found: set[str] = set()
    try:
        discovery_port = int(os.getenv("DISCOVERY_PORT", str(DISCOVERY_PORT)))
        if not 1 <= discovery_port <= 65535:
            raise ValueError("DISCOVERY_PORT must be between 1 and 65535")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery_socket:
            discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            discovery_socket.settimeout(0.2)
            sent = False
            for target in (
                ("255.255.255.255", discovery_port),
                ("127.0.0.1", discovery_port),
            ):
                try:
                    discovery_socket.sendto(DISCOVERY_REQUEST, target)
                    sent = True
                except OSError:
                    continue
            if not sent:
                return []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, (address, _) = discovery_socket.recvfrom(256)
                except TimeoutError:
                    continue
                if not data.startswith(DISCOVERY_RESPONSE_PREFIX):
                    continue
                try:
                    port = int(data[len(DISCOVERY_RESPONSE_PREFIX) :].decode("ascii"))
                except (UnicodeError, ValueError):
                    continue
                if 1 <= port <= 65535:
                    found.add(f"https://{address}:{port}")
    except OSError:
        return []
    return sorted(found)


def normalize_server_url(address: str) -> str:
    value = address.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme == "http":
        raise ValueError(
            "HTTPS is required: use https://HOST:PORT (plain HTTP is not supported)"
        )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a server address such as 192.168.1.20:8000")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Server port must be between 1 and 65535")
    except ValueError as exc:
        raise ValueError("Server address contains an invalid port") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def session_event_worker(
    base_url: str,
    session_id: int,
    access_token: str,
    events: Queue[dict[str, str]],
    stop_event: threading.Event,
) -> None:
    try:
        import websockets.sync.client  # noqa: F401
        from websockets.exceptions import WebSocketException
    except ImportError:
        events.put(
            {
                "type": "error",
                "text": "Live updates require the websockets package; install Ai/requirements.txt",
            }
        )
        return

    error_reported = False
    while not stop_event.is_set():
        try:
            with transport.open_event_stream(
                base_url, session_id, access_token
            ) as stream:
                error_reported = False
                while not stop_event.is_set():
                    event = stream.recv(timeout=1)
                    if isinstance(event, dict) and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in event.items()
                    ):
                        events.put(event)
        except (
            transport.UntrustedServer,
            transport.InsecureServerUrl,
            transport.ServerIdentityChanged,
        ) as exc:
            events.put({"type": "error", "text": str(exc)})
            return
        except (OSError, TimeoutError, WebSocketException, transport.TransportError) as exc:
            if stop_event.is_set():
                return
            if not error_reported:
                events.put({"type": "error", "text": str(exc)})
                error_reported = True
            stop_event.wait(3)


def parse_chat_log(chat_log: str) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    speaker: str | None = None
    timestamp: str | None = None
    message_lines: list[str] = []

    def save_message() -> None:
        nonlocal speaker, timestamp, message_lines
        if speaker is not None or message_lines:
            messages.append(
                TranscriptMessage(
                    speaker=speaker or "History",
                    timestamp=timestamp,
                    text="\n".join(message_lines).strip(),
                )
            )
        speaker = None
        timestamp = None
        message_lines = []

    for line in chat_log.splitlines():
        match = SPEAKER_LINE.match(line) if not line[:1].isspace() else None
        if match:
            save_message()
            speaker = match.group("speaker").strip()
            timestamp = match.group("timestamp")
            message_lines = [match.group("text") or ""]
        else:
            message_lines.append(line[2:] if line.startswith("  ") else line)
    save_message()
    return messages


def format_timestamp(timestamp: str | None) -> str:
    if timestamp is None:
        return "time unknown"
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    return parsed.astimezone().strftime("%b %d, %H:%M")


def style_message_text(text: str) -> list[tuple[str, bool]]:
    segments: list[tuple[str, bool]] = []
    position = 0
    for match in ACTION_MARKUP.finditer(text):
        if match.start() > position:
            segments.append((text[position : match.start()], False))
        marked_text = match.group(0)
        marker_size = 2 if marked_text.startswith("**") else 1
        segments.append((marked_text[marker_size:-marker_size], True))
        position = match.end()
    if position < len(text):
        segments.append((text[position:], False))
    return segments or [("", False)]


class ChatApp:
    def __init__(
        self,
        screen: curses.window,
        base_url: str,
        data_file: Path,
        workspace: Path | None = None,
    ) -> None:
        self.screen = screen
        self.base_url = base_url.strip()
        self.data_file = data_file
        transport.configure_trust_store(data_file.with_name("known_servers.json"))
        self.sessions = load_sessions(data_file)
        self.active_index = len(self.sessions) - 1
        self.input_text = ""
        self.input_cursor = 0
        self.action_query = ""
        self.session_hit_targets: list[tuple[int, int]] = []
        self.status = "Ready"
        self.scroll_offset = 0
        self.coding_scroll_offset = 0
        self.help_visible = False
        self.nav_help_visible = True
        self.active_tab = "Chat"
        self.view_mode = "chat"
        self.account_access_token = ""
        self.account_display_name = ""
        self.knowledge_sources: list[dict[str, Any]] = []
        self.action_menu_visible = False
        self.action_menu_index = 0
        self.action_menu_items = (
            ("Chat", "Conversation · F2"),
            ("Data", "Your private knowledge · F3"),
            ("Activity", "Live session progress · F4"),
            ("Coding", "Task brief and agent output · F5"),
            ("Review", "Inspect changes before applying · F6"),
            ("Split", "Chat alongside coding · F7"),
            ("New session", "Start a conversation · /new"),
            ("Help", "All commands and shortcuts · /help"),
            ("/login", "Join a session by ID"),
            ("/account login", "Sign in to your local account"),
            ("/account logout", "Sign out of your local account"),
            ("/knowledge list", "Refresh your knowledge library"),
            ("/knowledge add ", "Add a local file · enter its path"),
            ("/code ", "Prepare a coding task · describe your request"),
            ("/run", "Run your reviewed brief with the coding agent"),
            ("/cancel", "Stop the coding agent"),
            ("/diff", "Inspect a pending patch"),
            ("/apply", "Apply the reviewed patch to your project"),
            ("/discard", "Discard the isolated patch"),
            ("/workspace", "Choose your project folder"),
            ("/agent list", "Check available coding agents"),
            ("/agent use ", "Choose copilot, claude, or codex"),
            ("/style", "Change the reply style"),
            ("/chatskill list", "Explore chat behaviours"),
            ("/export", "Export this conversation to a local file"),
            ("/connect", "Connect to a different server"),
            ("/setup", "Set up your profile"),
            ("/retry cancel", "Cancel a waiting message retry"),
            ("/quit", "Close Super Bot"),
        )
        self.tab_hit_targets: list[tuple[str, int, int]] = []
        self.mouse_enabled = False
        self.future: Future[dict[str, Any]] | None = None
        self.pending_kind = ""
        self.pending_session_data: tuple[str, str] | None = None
        self.pending_initial_chat_log: str | None = None
        self.pending_profile_name = ""
        self.pending_message = ""
        self.pending_message_session_id: int | None = None
        self.pending_message_chat_context = ""
        self.pending_message_coding_context = ""
        self.pending_message_can_start_coding = False
        self.message_progress = ""
        self.progress_updates: dict[str, ProgressUpdate] = {}
        self.coding_only_mode = False
        self.code_only_queue: list[str] = []
        self.pending_model_fallback_task = ""
        self.retry_delay_seconds = 5
        self.retry_due: float | None = None
        self.coding_data_path = data_file.with_name("coding_state.json")
        self.profiles_path = data_file.with_name("chat_profiles.json")
        self.setup_state_path = data_file.with_name("setup_state.json")
        self.ui_preferences_path = data_file.with_name("ui_preferences.json")
        self.chat_skills_path = data_file.with_name("chat_skills.json")
        self.chat_skill_library = load_chat_skills(self.chat_skills_path)
        self.profile_store = load_profiles(self.profiles_path)
        self.setup_state = load_setup_state(self.setup_state_path)
        self.ui_preferences = load_ui_preferences(self.ui_preferences_path)
        self.nav_help_visible = self.ui_preferences.show_navigation_help
        self.coding_state = coding_tools.load_coding_state(self.coding_data_path)
        default_workspace = Path(__file__).resolve().parent.parent
        configured_workspace = workspace or (
            Path(self.coding_state.workspace)
            if self.coding_state.workspace
            else default_workspace
        )
        self.workspace = configured_workspace.expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.workspace}")
        self.coding_state.workspace = str(self.workspace)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.session_events: Queue[dict[str, str]] = Queue()
        self.session_event_stop = threading.Event()
        self.session_event_thread: threading.Thread | None = None
        if self.coding_state.job_status == "running":
            self.coding_state.job_status = "interrupted"
        if self.coding_state.job_status == "preparing":
            self.coding_state.job_status = "interrupted"
        if self.coding_state.plan_status in {"planning", "refining", "suggesting"}:
            self.coding_state.plan_status = "interrupted"
        self.skills = coding_tools.discover_skills(
            self.workspace,
            self.coding_state.custom_skills,
        )
        self.coding_future: Future[Any] | None = None
        self.coding_future_kind = ""
        self.coding_request_chat_context = ""
        self.agent_process: coding_tools.AgentProcess | None = None
        self.agent_output_lines: list[str] = []
        self.pending_command_argv: list[str] | None = None
        self.command_confirmation = bool(self.coding_state.proposed_command)
        if self.command_confirmation:
            self.coding_state.command_cwd = str(self.workspace)
        self.closed = False
        self._set_startup_status()
        self._save_coding_state()
        self._setup_screen()

    def _set_startup_status(self) -> None:
        providers = coding_tools.discover_providers()
        installed = [p["id"] for p in providers if p["available"]]
        if installed:
            self.status = (
                "Coding CLIs detected: "
                + ", ".join(str(provider) for provider in installed)
                + ". Use /agent list."
            )
        else:
            self.status = "No coding CLI detected; Copilot is the default. Use /agent install copilot."
        if self.command_confirmation:
            self.status = "A saved AI command awaits your explicit /approve or /reject."

    def _connect_to_server(self) -> bool:
        previous_url = self.base_url
        if not self._select_server():
            return False
        if self._verify_server(self.base_url):
            return True
        self.base_url = previous_url
        return False

    def _verify_server(self, url: str) -> bool:
        self.status = f"Verifying {url}…"
        try:
            check = transport.check_server(url)
        except transport.TransportError as exc:
            self.status = f"Error: {exc}"
            return False
        if check.state == "changed":
            self._confirm_lines(
                "SERVER IDENTITY CHANGED",
                [
                    check.reason,
                    "Someone may be intercepting your connection, or the server's keys "
                    "were reset. Compare with the fingerprints the server printed:",
                    *transport.describe_server(check.info),
                    "If you expected this, close this screen, use /server forget, then "
                    "/connect again.",
                ],
                "Connection refused. Press any key to continue. ",
                4,
            )
            self.status = "Error: the server's identity changed; connection refused"
            return False
        if check.state == "new":
            if not self._confirm_lines(
                "VERIFY SERVER",
                [
                    "First connection to this server. Compare these fingerprints with "
                    "the ones the server printed when it started:",
                    *transport.describe_server(check.info),
                ],
                "Trust this server and remember it? [y/N] ",
                3,
            ):
                self.status = "Server was not trusted; connection cancelled"
                return False
            try:
                transport.trust_store().pin(url, check.info)
            except (OSError, transport.TransportError) as exc:
                self.status = f"Error: could not save the trusted server: {exc}"
                return False
        self.status = f"Connected to {url} (verified, end-to-end encrypted)"
        return True

    def _confirm_lines(
        self,
        title: str,
        lines: list[str],
        prompt: str,
        color: int,
    ) -> bool:
        height, width = self.screen.getmaxyx()
        text_width = max(10, width - 4)
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, f" {title} ", curses.A_BOLD | self._color(color))
            row = 2
            for line in lines:
                for wrapped in textwrap.wrap(line, text_width, subsequent_indent="  ") or [""]:
                    if row >= height - 3:
                        break
                    self._put(row, 2, wrapped)
                    row += 1
            prompt_row = min(height - 2, row + 1)
            self._put(prompt_row, 2, prompt[: max(1, width - 4)])
            self.screen.move(prompt_row, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _handle_server_command(self, argument: str) -> None:
        session = self.active_session
        url = (session.server_url if session is not None else "") or self.base_url
        if not url:
            self.status = "Error: not connected to a server"
            return
        action = argument.casefold()
        if action not in {"", "forget"}:
            self.status = "Error: use /server or /server forget"
            return
        try:
            address = transport.parse_server_url(url).key
            if action == "forget":
                forgotten = transport.trust_store().forget(url)
                self.status = (
                    f"Forgot {address}; use /connect to verify it again"
                    if forgotten
                    else f"{address} was not verified, so there is nothing to forget"
                )
                return
            pinned = transport.trust_store().get(url)
        except transport.TransportError as exc:
            self.status = f"Error: {exc}"
            return
        if pinned is None:
            self.status = f"{address} is not verified yet; use /connect"
            return
        self._confirm_lines(
            "SERVER",
            transport.describe_pinned(url, pinned),
            "Press any key to continue. ",
            1,
        )
        self.status = f"{address}: verified, HTTPS and end-to-end encryption"

    def _select_server(self) -> bool:
        try:
            servers = discover_servers()
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return False
        selected = self._choose_discovered_server(servers)
        if selected is None:
            self.status = "Server connection cancelled"
            return False
        if selected:
            self.base_url = selected
            self.status = f"Selected server {self.base_url}"
            return True

        address = self._prompt(
            "HTTPS address, IP, or hostname (for example 192.168.1.20:8000)",
            title="MANUAL SERVER ADDRESS",
        )

        try:
            server_url = normalize_server_url(address)
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return False
        if not server_url:
            self.status = "Server connection cancelled"
            return False
        self.base_url = server_url
        self.status = f"Selected manual server {self.base_url}"
        return True

    def _choose_discovered_server(self, servers: list[str]) -> str | None:
        """Return a chosen address, an empty manual-selection marker, or None."""
        selected = 0
        options = [*servers, ""]
        feedback = ""
        self.screen.timeout(-1)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.screen.erase()
                self._put(
                    0,
                    2,
                    " SUPER BOT  /  CONNECT ",
                    curses.A_BOLD | self._color(1),
                )
                self._put(
                    2,
                    2,
                    "Choose a server discovered on your local network.",
                )
                self._put(
                    3,
                    2,
                    "Or choose Manual address for a server outside this network.",
                    curses.A_DIM,
                )
                self._put(5, 2, " DISCOVERED SERVERS ", curses.A_BOLD | self._color(2))
                row = 6
                if servers:
                    for index, server in enumerate(servers):
                        marker = "> " if index == selected else "  "
                        self._put(
                            row,
                            3,
                            f"{marker}{server}"[: max(1, width - 6)],
                            curses.A_REVERSE if index == selected else 0,
                        )
                        row += 1
                else:
                    self._put(row, 3, "No local servers found.", curses.A_DIM)
                    row += 1
                row = min(row + 1, height - 4)
                self._put(row, 2, " OTHER SERVER ", curses.A_BOLD | self._color(3))
                manual_index = len(servers)
                marker = "> " if selected == manual_index else "  "
                self._put(
                    row + 1,
                    3,
                    f"{marker}Manual address  ·  enter an HTTPS hostname or IP",
                    curses.A_REVERSE if selected == manual_index else 0,
                )
                self._put(
                    height - 2,
                    2,
                    "↑/↓ Select  ·  Enter Connect  ·  / Command  ·  Esc Back",
                    curses.A_DIM,
                )
                if feedback:
                    self._put(height - 1, 2, feedback)
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in (curses.KEY_UP, "k"):
                    selected = (selected - 1) % len(options)
                elif key in (curses.KEY_DOWN, "j"):
                    selected = (selected + 1) % len(options)
                elif key in ("\n", "\r", curses.KEY_ENTER):
                    return options[selected]
                elif key == "/":
                    try:
                        command = self._prompt(
                            "Command: /connect ADDRESS, /server forget, /refresh, /help, /cancel",
                            title="SERVER COMMAND",
                        ).strip().removeprefix("/")
                    finally:
                        self.screen.timeout(-1)
                    action, _, argument = command.partition(" ")
                    action = action.casefold()
                    argument = argument.strip()
                    if action == "connect":
                        if not argument:
                            return ""
                        try:
                            return normalize_server_url(argument)
                        except ValueError as exc:
                            feedback = str(exc)
                    elif action == "server" and argument.casefold() == "forget":
                        url = options[selected]
                        if not url:
                            feedback = "Highlight a discovered server before using /server forget."
                        else:
                            try:
                                forgotten = transport.trust_store().forget(url)
                            except transport.TransportError as exc:
                                feedback = str(exc)
                            else:
                                feedback = (
                                    "Forgot server trust. Verify its fingerprints on reconnect."
                                    if forgotten else "This server has no saved trust."
                                )
                    elif action == "refresh" and not argument:
                        try:
                            servers = discover_servers()
                        except ValueError as exc:
                            feedback = str(exc)
                        else:
                            options = [*servers, ""]
                            selected = 0
                            feedback = f"Found {len(servers)} local server(s)."
                    elif action == "cancel" and not argument:
                        return None
                    elif command:
                        feedback = "Commands: /connect ADDRESS, /server forget, /refresh, /help, /cancel"
                elif key == "\x1b":
                    return None
        except curses.error:
            return None
        finally:
            self.screen.timeout(100)

    def _start_session_events(self) -> None:
        self._stop_session_events()
        self.progress_updates.clear()
        session = self.active_session
        if session is None or not session.access_token:
            return
        self.session_events = Queue()
        self.session_event_stop = threading.Event()
        self.session_event_thread = threading.Thread(
            target=session_event_worker,
            args=(
                session.server_url or self.base_url,
                session.server_session_id,
                session.access_token,
                self.session_events,
                self.session_event_stop,
            ),
            name=f"session-events-{session.server_session_id}",
            daemon=True,
        )
        self.session_event_thread.start()

    def _stop_session_events(self) -> None:
        self.session_event_stop.set()
        self.session_event_thread = None

    def _active_participant_id(self) -> str:
        session = self.active_session
        if session is None or not session.access_token:
            return ""
        return hashlib.sha256(session.access_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _format_progress(phase: str, detail: str = "") -> str:
        label = PROGRESS_LABELS.get(phase, "Working")
        return f"{label} · {detail}" if detail else label

    def _poll_session_events(self) -> None:
        now = time.monotonic()
        for participant_id, update in tuple(self.progress_updates.items()):
            ttl = (
                PROGRESS_TERMINAL_TTL_SECONDS
                if update.phase in TERMINAL_PROGRESS_PHASES
                else PROGRESS_ACTIVE_TTL_SECONDS
            )
            if now - update.updated_at >= ttl:
                del self.progress_updates[participant_id]
        while True:
            try:
                event = self.session_events.get_nowait()
            except Empty:
                return
            event_type = event.get("type", "")
            if event_type == "progress":
                participant_id = event.get("participant_id", "")
                phase = event.get("phase", "")
                player_name = event.get("player_name", "A client")
                if participant_id not in self.progress_updates:
                    self.progress_updates[participant_id] = ProgressUpdate(
                        participant_id=participant_id,
                        player_name=player_name,
                        phase=phase,
                        detail=event.get("detail", ""),
                        updated_at=time.monotonic(),
                    )
                else:
                    update = self.progress_updates[participant_id]
                    update.phase = phase
                    update.detail = event.get("detail", "")
                    update.updated_at = time.monotonic()
                    update.player_name = player_name
                if participant_id == self._active_participant_id():
                    self.message_progress = self._format_progress(phase, event.get("detail", ""))
            elif event_type == "message":
                if self.future is not None and self.pending_kind in {"message", "message_retry", "login", "new"}:
                    # The network worker holds the client session lock. Leave
                    # live exchanges queued rather than blocking the UI on it.
                    self.session_events.put(event)
                    return
                session = self.active_session
                chat_log = None
                if session is not None:
                    try:
                        chat_log = client_data.local_history(
                            session.server_url or self.base_url,
                            session.server_session_id,
                            event,
                        )
                    except (OSError, client_data.database.DatabaseError) as exc:
                        self.status = f"Error saving shared chat update: {exc}"
                        continue
                    if chat_log is None:
                        # Legacy clients may not yet have a client.db record.
                        # Never replace their complete history with a bounded tail.
                        incoming = event.get("chat_log")
                        if isinstance(incoming, str) and incoming.startswith(session.chat_log):
                            chat_log = incoming
                if (
                    session is not None
                    and isinstance(chat_log, str)
                    and session.chat_log != chat_log
                ):
                    session.chat_log = chat_log
                    try:
                        save_sessions(self.data_file, self.sessions)
                    except OSError as exc:
                        self.status = f"Error saving shared chat update: {exc}"
                    else:
                        if self._sync_profile_chat_log(session):
                            self.status = (
                                f"{event.get('player_name', 'A client')} sent a message"
                            )
            elif event_type == "joined":
                self.status = f"{event.get('player_name', 'A client')} joined this session"
            elif event_type == "error":
                self.status = f"Live update connection interrupted: {event.get('text', '')}"

    def _startup_login(self) -> bool:
        while not self.closed:
            login = self._login_form()
            if login is None:
                self.status = "Login cancelled"
                return False
            session_text, password = login
            if session_text.casefold() == "new":
                self.create_session()
                self._wait_for_startup_request()
                return self.active_session is not None or self.coding_only_mode
            if not session_text.isdigit() or int(session_text) < 1:
                self.status = "Error: session ID must be a positive integer, or type NEW"
                continue

            session_id = int(session_text)
            self.status = f"Authenticating to server session {session_id}…"
            self.pending_kind = "login"
            self.pending_session_data = (str(session_id), "")
            self.future = self.executor.submit(
                login_remote_session,
                self.base_url,
                session_id,
                password,
            )
            self._wait_for_startup_request()
            if self.active_session is not None:
                return True
        return False

    def _login_form(self) -> tuple[str, str] | None:
        """Start a new chat or join one, keeping its password in memory only."""
        session_id = ""
        password = ""
        field = 0
        self.screen.timeout(-1)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.screen.erase()
                if height < 12 or width < 40:
                    self._put(0, 0, "Resize to 40 × 12 · Esc to close")
                    self.screen.refresh()
                    if self.screen.get_wch() in ("\x1b", "\x03"):
                        return None
                    continue
                panel_width = min(72, width - 4)
                left = (width - panel_width) // 2
                top = max(1, (height - 17) // 2)
                self._put(top, left + 2, "SUPER BOT", curses.A_BOLD | self._color(1))
                self._put(top + 1, left + 2, "Start a conversation", curses.A_BOLD)
                compact = height < 18
                if not compact:
                    self._put(top + 3, left + 2, tui.clip("Enter starts a new chat. To join, enter a session ID.", panel_width - 4), curses.A_DIM)
                first_row = top + (3 if compact else 5)
                second_row = first_row + (2 if compact else 4)
                for number, (label, value, row) in enumerate((
                    ("Session ID", session_id, first_row),
                    ("Password · optional", "*" * len(password), second_row),
                )):
                    attr = self._color(1) if field == number else self._color(5)
                    if not compact:
                        self._box(row, left, 3, panel_width, attr)
                        self._put(row, left + 2, " " + label + " ", attr)
                    else:
                        self._put(row, left + 2, label, attr)
                    placeholder = "NEW · press Enter to start" if number == 0 else "Leave blank if the session has no password"
                    visible, _ = tui.editor_view(value, len(value), panel_width - 6)
                    self._put(row + 1, left + 3, tui.clip(visible or placeholder, panel_width - 6), curses.A_BOLD if value else curses.A_DIM)
                status_row = height - 3
                status = self.status if self.status.startswith("Error:") else "History stays on this device. Server context is temporary."
                self._put(status_row, left, tui.clip(status, panel_width), self._color(4) if self.status.startswith("Error:") else self._color(5))
                self._put(height - 2, left, tui.clip("Enter continue · Tab switch field · Esc quit", panel_width), curses.A_DIM)
                cursor_value = session_id if field == 0 else password
                _, cursor = tui.editor_view(cursor_value, len(cursor_value), panel_width - 6)
                try:
                    curses.curs_set(1)
                    self.screen.move((first_row if field == 0 else second_row) + 1, left + 3 + cursor)
                except curses.error:
                    pass
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\x1b", "\x03"):
                    return None
                if key in ("\t", curses.KEY_UP, curses.KEY_DOWN, curses.KEY_BTAB):
                    field = 1 - field
                elif key in ("\n", "\r", curses.KEY_ENTER):
                    if not session_id.strip() or session_id.strip().casefold() == "new":
                        return "NEW", ""
                    if field == 0:
                        field = 1
                    else:
                        return session_id.strip(), password
                elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    if field == 0:
                        session_id = session_id[:-1]
                    else:
                        password = password[:-1]
                elif isinstance(key, str) and key.isprintable():
                    if field == 0 and len(session_id) < 20:
                        session_id += key
                    elif field == 1 and len(password) < 128:
                        password += key
        except curses.error:
            return None
        finally:
            self.screen.timeout(100)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _wait_for_startup_request(self) -> None:
        while self.future is not None:
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            self._put(0, 2, " SUPER BOT  /  LOGIN ", curses.A_BOLD | self._color(1))
            self._put(height // 2, 2, self.status[: max(1, width - 4)])
            self.screen.refresh()
            self._finish_pending_request()
            if self.future is not None:
                try:
                    self.screen.get_wch()
                except curses.error:
                    pass

    def _save_coding_state(self) -> None:
        try:
            coding_tools.save_coding_state(self.coding_data_path, self.coding_state)
        except OSError as exc:
            self.status = f"Error saving coding state: {exc}"

    def _save_ui_preferences(self) -> None:
        try:
            save_ui_preferences(self.ui_preferences_path, self.ui_preferences)
        except OSError as exc:
            self.status = f"Error saving UI preferences: {exc}"

    def _save_setup_state(self) -> bool:
        try:
            save_setup_state(self.setup_state_path, self.setup_state)
        except OSError as exc:
            self.status = f"Error saving setup state: {exc}"
            return False
        return True

    def _run_setup_wizard(self) -> None:
        """Create a safe local profile on first use; provider credentials stay server-side."""
        if self.setup_state.completed:
            return
        start = self._confirm_lines(
            "SETUP WIZARD",
            [
                "Set up a local profile for new chats.",
                "Your selected model is a preference only: the server owner configures "
                "the actual provider and keeps API keys private.",
                "Character notes are opt-in local context for future sessions; Super Bot "
                "does not learn from chats automatically.",
            ],
            "Start setup? [y/N] ",
            1,
        )
        if not start:
            self.setup_state.completed = True
            self._save_setup_state()
            self.status = "Setup skipped. Run /setup later to create a profile."
            return

        profile_name = self._prompt("Profile name", title="SETUP WIZARD")
        player_name = self._prompt("Player name", title="SETUP WIZARD")
        persona = self._prompt("AI persona", title="SETUP WIZARD")
        if not profile_name or not player_name or not persona:
            self.status = "Setup cancelled; no profile was saved."
            return
        model_prompt = (
            "Preferred model: 1) Super Bot Chatter (local), "
            "2) existing local model, 3) Gemini (server configured)"
        )
        choice = self._prompt(model_prompt, title="SETUP WIZARD")
        selected_model = MODEL_CHOICES[int(choice) - 1] if choice in {"1", "2", "3"} else ""
        if choice and not selected_model:
            self.status = "Setup cancelled: model choice must be 1, 2, or 3."
            return
        character_notes = self._prompt(
            "Optional character notes (blank disables character development)",
            title="SETUP WIZARD",
        )
        if len(character_notes) > MAX_CHARACTER_NOTES_LENGTH:
            self.status = (
                f"Setup cancelled: character notes must be at most "
                f"{MAX_CHARACTER_NOTES_LENGTH} characters."
            )
            return
        self.profile_store.profiles[profile_name] = ChatProfile(
            player_name=player_name,
            persona=persona,
            preferred_model=selected_model,
            character_development_enabled=bool(character_notes),
            character_notes=character_notes,
        )
        self.profile_store.selected = profile_name
        self.setup_state = SetupState(completed=True, preferred_model=selected_model)
        if self._save_profile_store() and self._save_setup_state():
            self.status = f"Setup complete. Selected profile: {profile_name}."

    def _setup_screen(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            self.mouse_enabled = bool(curses.mousemask(curses.ALL_MOUSE_EVENTS)[0])
        except curses.error:
            self.mouse_enabled = False
        if curses.has_colors():
            curses.start_color()
            background = -1
            try:
                curses.use_default_colors()
            except curses.error:
                background = curses.COLOR_BLACK
            # Muted teal, mint, amber and coral on the terminal's own background.
            colors = (81, 114, 221, 210, 245, 153) if curses.COLORS >= 256 else (
                curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_BLUE,
            )
            for pair, foreground in enumerate(colors, 1):
                curses.init_pair(pair, foreground, background)

    @property
    def active_session(self) -> ChatSession | None:
        if 0 <= self.active_index < len(self.sessions):
            return self.sessions[self.active_index]
        return None

    def run(self) -> None:
        try:
            if not self.base_url and not self._connect_to_server():
                return
            if hasattr(self, "setup_state"):
                self._run_setup_wizard()
            if not self._startup_login():
                return
            while not self.closed:
                self._finish_pending_request()
                self._start_due_message_retry()
                self._finish_coding_request()
                self._poll_agent()
                self._poll_session_events()
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                self.handle_key(key)
        finally:
            client_data.close_all()
            self._stop_session_events()
            if self.agent_process is not None:
                self.agent_process.cancel()
                self.coding_state.job_status = "interrupted"
                self._save_coding_state()
            self.executor.shutdown(wait=False, cancel_futures=True)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.session_hit_targets = []
        self.tab_hit_targets = []
        if height < 12 or width < 40:
            self._put(0, 0, "Super Bot · resize to 40 × 12", curses.A_BOLD)
            self._put(2, 0, "Your draft is kept. Ctrl+C exits.", curses.A_DIM)
            self.screen.refresh()
            return

        session = self.active_session
        self._put(0, 2, "SUPER BOT", curses.A_BOLD | self._color(1))
        self._put(0, 13, " / " + ("Command guide" if self.help_visible else self.active_tab), curses.A_DIM)
        badge = "LOCAL HISTORY"
        if width >= 64:
            self._put(0, width - len(badge) - 3, badge, self._color(2))
        subtitle = (
            f"{session.player_name} · Session {session.server_session_id}"
            if session else "Your space to talk, build, and explore"
        )
        if self.coding_only_mode:
            subtitle = f"Coding directly with {self.coding_state.provider} · {self.workspace.name}"
        self._put(1, 2, tui.clip(subtitle, width - 4), self._color(5))
        self._draw_tabs(width)
        self._put(3, 1, "─" * (width - 2), self._color(5))

        body_top = 4
        show_extra_hints = self.nav_help_visible and height >= 18
        footer_top = height - (7 if show_extra_hints else 6)
        body_height = max(1, footer_top - body_top)
        sidebar = width >= 110 and self.view_mode != "split" and not self.help_visible
        left = 31 if sidebar else 3
        content_width = width - left - 3
        if sidebar:
            self._draw_sidebar(body_top, body_height, 27)
            for row in range(body_top, footer_top):
                self._put(row, 28, "│", self._color(5))
        if self.help_visible:
            lines = [[(line, "normal")] for line in HELP_TEXT.splitlines()]
            self._draw_panel(lines, body_top, left, body_height, content_width, "scroll_offset")
        elif self.active_tab == "Chat" and self.view_mode == "split" and width >= 90:
            column_width = (width - 7) // 2
            self._put(body_top, 3, "CONVERSATION", curses.A_BOLD | self._color(1))
            self._put(body_top, column_width + 6, "CODING ACTIVITY", curses.A_BOLD | self._color(2))
            for row in range(body_top, footer_top):
                self._put(row, column_width + 4, "│", self._color(5))
            self._draw_panel(self._chat_panel_lines(column_width), body_top + 1, 3,
                             body_height - 1, column_width, "scroll_offset")
            self._draw_panel(self._coding_panel_lines(column_width), body_top + 1,
                             column_width + 6, body_height - 1, column_width,
                             "coding_scroll_offset")
        else:
            panels = {
                "Chat": lambda: self._chat_panel_lines(content_width),
                "Coding": lambda: self._coding_panel_lines(content_width),
                "Activity": self._activity_panel_lines,
                "Data": lambda: self._data_panel_lines(content_width),
                "Review": self._review_panel_lines,
            }
            offset = "coding_scroll_offset" if self.active_tab in {"Coding", "Review"} else "scroll_offset"
            self._draw_panel(panels[self.active_tab](), body_top, left, body_height,
                             content_width, offset)

        progress = self.message_progress if self.ui_preferences.show_progress else ""
        busy = self.future is not None or self.coding_future is not None or self.agent_process is not None
        status = progress or self.status
        if self.help_visible:
            status = "Command guide · Esc returns to your conversation"
        failed = status.casefold().startswith(("error", "request failed", "failed"))
        marker = "! " if failed else ("◐◓◑◒"[int(time.monotonic() * 5) % 4] + " " if busy else "● ")
        self._put(footer_top, 2, tui.clip(marker + status, width - 4, True), self._color(4 if failed else 3 if busy else 2))

        label = "Command" if self.input_text.startswith("/") else "Task" if self.coding_only_mode else "Message"
        limit = 4000 if self.coding_only_mode else 1000
        count = str(len(self.input_text)) if label == "Command" else f"{len(self.input_text)}/{limit}"
        self._box(footer_top + 1, 1, 3, width - 2, self._color(1))
        self._put(footer_top + 1, 3, f" {label} ", curses.A_BOLD | self._color(1))
        self._put(footer_top + 1, width - len(count) - 5, f" {count} ", self._color(4) if label != "Command" and len(self.input_text) > limit else self._color(5))
        input_row = footer_top + 2
        self.input_cursor = min(getattr(self, "input_cursor", len(self.input_text)), len(self.input_text))
        visible, cursor = tui.editor_view(self.input_text, self.input_cursor, width - 8)
        self._put(input_row, 3, "›", self._color(1))
        placeholder = "Describe your coding task…" if self.coding_only_mode else "Write a message, or / for commands…"
        self._put(input_row, 5, visible or tui.clip(placeholder, width - 8), 0 if visible else curses.A_DIM)
        hint = "Enter send  ·  Ctrl+P actions  ·  Ctrl+O chats"
        if self.help_visible:
            hint = "↑ ↓ scroll  ·  Home top  ·  Esc close"
        elif self.action_menu_visible:
            hint = "Type to search  ·  ↑ ↓ select  ·  Enter choose"
        elif self.active_tab == "Review":
            hint = "Ctrl+A apply  ·  Ctrl+D discard  ·  Ctrl+P actions"
        elif self.input_text.startswith("/"):
            hint = "Tab find command  ·  Enter run  ·  Ctrl+U clear"
        if width < 60:
            hint = "Esc close · ↑ ↓ scroll" if self.help_visible else "Enter select · Esc close" if self.action_menu_visible else "Enter send · Ctrl+P actions"
        self._put(footer_top + 4, 2, tui.clip(hint, width - 4), curses.A_DIM)
        if show_extra_hints:
            self._put(footer_top + 5, 2, tui.clip("Tab views  ·  PgUp/PgDn scroll  ·  F1 hints  ·  Ctrl+C quit", width - 4), curses.A_DIM)
        if self.action_menu_visible:
            self._draw_action_menu(1, height - 2, width)
        try:
            curses.curs_set(0 if self.action_menu_visible or self.help_visible else 1)
            if not self.action_menu_visible and not self.help_visible:
                self.screen.move(input_row, min(width - 3, 5 + cursor))
        except curses.error:
            pass
        self.screen.refresh()

    def _box(self, top: int, left: int, height: int, width: int, attr: int = 0) -> None:
        if height < 2 or width < 2:
            return
        self._put(top, left, "╭" + "─" * (width - 2) + "╮", attr)
        for row in range(top + 1, top + height - 1):
            self._put(row, left, "│", attr)
            self._put(row, left + width - 1, "│", attr)
        self._put(top + height - 1, left, "╰" + "─" * (width - 2) + "╯", attr)

    def _draw_panel(self, lines: list[list[tuple[str, str]]], top: int, left: int,
                    height: int, width: int, offset_name: str) -> None:
        if height < 1:
            return
        wrapped = [part for line in lines for part in self._wrap_rich_segments(line, max(1, width - 1))]
        while wrapped and not any(text.strip() for text, _ in wrapped[-1]):
            wrapped.pop()
        offset = min(getattr(self, offset_name), max(0, len(wrapped) - height))
        setattr(self, offset_name, offset)
        end = max(0, len(wrapped) - offset)
        start = max(0, end - height)
        for row, line in enumerate(wrapped[start:end], top):
            self._draw_rich_line(row, left, line, width - 1)
        if len(wrapped) > height:
            thumb = max(1, height * height // len(wrapped))
            position = (height - thumb) * start // max(1, len(wrapped) - height)
            for row in range(height):
                self._put(top + row, left + width - 1, "┃" if position <= row < position + thumb else "│", self._color(1) if position <= row < position + thumb else self._color(5))

    def _draw_sidebar(self, top: int, height: int, width: int) -> None:
        self._put(top, 2, "CONVERSATIONS", curses.A_BOLD | self._color(5))
        if height < 4:
            return
        self._put(top + 1, 2, "+ New conversation", self._color(1))
        self.session_hit_targets.append((top + 1, -1))
        available = max(0, (height - 7) // 3)
        first = max(0, min(self.active_index - available // 2, len(self.sessions) - available))
        for index in range(first, min(len(self.sessions), first + available)):
            session = self.sessions[index]
            row = top + 3 + (index - first) * 3
            selected = index == self.active_index
            title = f"{'›' if selected else ' '} {index + 1}. {session.player_name}"
            attr = curses.A_BOLD | self._color(1) if selected else 0
            self._put(row, 1, tui.pad(tui.clip(title, width - 2, True), width - 1), attr | (curses.A_REVERSE if selected else 0))
            self._put(row + 1, 4, tui.clip(session.persona or "Conversation", width - 5, True), curses.A_DIM)
            self.session_hit_targets.extend(((row, index), (row + 1, index)))
        if not self.sessions and available:
            self._put(top + 3, 2, "Your chats appear here.", curses.A_DIM)
        if height >= 7:
            self._put(top + height - 3, 2, "Ctrl+O  All conversations", self._color(5))
            self._put(top + height - 2, 2, tui.clip(self.workspace.name, width - 3, True), curses.A_DIM)
            self._put(top + height - 1, 2, tui.clip(f"Agent · {self.coding_state.provider}", width - 3), self._color(2))

    def _draw_tabs(self, width: int) -> None:
        self.tab_hit_targets = []
        column = 2
        for index, tab in enumerate(TABS):
            name = {"Activity": "Live", "Coding": "Code", "Review": "Diff"}.get(tab, tab) if width < 55 else tab
            label = f" {name}" + (f" F{index + 2} " if width >= 76 else " ")
            selected = tab == self.active_tab
            attr = curses.A_BOLD | self._color(1) | curses.A_REVERSE if selected else self._color(5)
            self._put(2, column, label, attr)
            self.tab_hit_targets.append((tab, column, column + len(label)))
            column += len(label) + 1

    def _filtered_actions(self) -> list[tuple[str, str]]:
        items = list(self.action_menu_items)
        items.extend((f"/open {index + 1}", f"Conversation · {session.player_name} · {session.persona[:80]}")
                     for index, session in enumerate(self.sessions))
        words = getattr(self, "action_query", "").casefold().split()
        return [(name, description) for name, description in items
                if all(word in (name + " " + description).casefold() for word in words)]

    def _open_actions(self, query: str = "") -> None:
        self.action_query = query
        self.action_menu_index = 0
        self.action_menu_visible = True

    def _draw_action_menu(self, top: int, body_height: int, width: int) -> None:
        items = self._filtered_actions()
        menu_width = min(76, width - 4)
        left = (width - menu_width) // 2
        menu_height = min(max(1, len(items)) + 6, body_height)
        menu_top = top + max(0, (body_height - menu_height) // 3)
        for row in range(menu_top, menu_top + menu_height):
            self._put(row, left, " " * menu_width)
        self._box(menu_top, left, menu_height, menu_width, self._color(1))
        self._put(menu_top, left + 2, " QUICK ACTIONS ", curses.A_BOLD | self._color(1))
        self._put(menu_top + 1, left + 2, tui.clip("› " + (self.action_query or "Search actions, commands, conversations…"), menu_width - 4), 0 if self.action_query else curses.A_DIM)
        self._put(menu_top + 2, left + 1, "─" * (menu_width - 2), self._color(5))
        visible_count = max(1, menu_height - 6)
        self.action_menu_index = min(self.action_menu_index, max(0, len(items) - 1))
        first = max(0, self.action_menu_index - visible_count + 1)
        for offset, (name, description) in enumerate(items[first:first + visible_count]):
            selected = first + offset == self.action_menu_index
            name_width = min(22, (menu_width - 6) // 2)
            text = ("› " if selected else "  ") + tui.pad(tui.clip(name, name_width, True), name_width) + "  " + description
            self._put(menu_top + 3 + offset, left + 1, tui.pad(tui.clip(text, menu_width - 2), menu_width - 2), curses.A_REVERSE | curses.A_BOLD if selected else 0)
        if not items:
            self._put(menu_top + 3, left + 3, "No matches. Try another word.", self._color(3))
        self._put(menu_top + menu_height - 2, left + 2, tui.clip("↑ ↓ choose · Enter select · Esc close", menu_width - 4), curses.A_DIM)

    def _chat_panel_lines(self, width: int) -> list[list[tuple[str, str]]]:
        session = self.active_session
        staged_chat_log = self.pending_initial_chat_log
        selected_profile = self.profile_store.profiles.get(
            self.profile_store.selected
        )
        if staged_chat_log is not None:
            if not staged_chat_log:
                return [[("No starting chat history is staged.", "dim")]]
            player_name = (
                session.player_name
                if session is not None
                else selected_profile.player_name
                if selected_profile is not None
                else "Player"
            )
            preview = ChatProfile(
                player_name=player_name,
                persona="",
                chat_log=staged_chat_log,
            )
            return [
                [("STARTING CHAT LOG — staged for /new", "player")],
                *self._transcript_lines(preview, width - 2),
            ]
        if session is not None and session.chat_log:
            lines = self._transcript_lines(session, width - 2)
            if self.pending_message and self.pending_message_session_id == session.server_session_id:
                lines.append([("● You · sending", "player")])
                lines.extend(self._wrap_rich_segments([(self.pending_message, "dim")], width - 2))
                lines.append([("  Waiting for Super Bot…", "dim")])
            return lines
        if session is None and selected_profile is not None:
            if selected_profile.chat_log:
                return [
                    [("SELECTED PROFILE CHAT LOG", "player")],
                    *self._transcript_lines(selected_profile, width - 2),
                ]
        if session is None:
            if self.coding_only_mode:
                return [
                    [("Code-only mode is ready.", "player")],
                    [("Describe a coding task below and press Enter.", "dim")],
                ]
            return [
                [("Welcome to your workspace", "player")],
                [("", "normal")],
                [("Talk through an idea, learn something, or build with a coding agent.", "normal")],
                [("", "normal")],
                [("/new       Start a conversation", "ai")],
                [("/login     Join a shared session", "ai")],
                [("Ctrl+O     Find a saved conversation", "ai")],
                [("Ctrl+P     Search every action", "ai")],
                [("", "normal")],
                [("Your saved conversations stay on this device.", "dim")],
            ]
        return [
            [("No messages yet.", "player")],
            [("Type below and press Enter to send.", "dim")],
        ]

    def _activity_panel_lines(self) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = [
            [("SESSION ACTIVITY", "player")],
            [("Progress is temporary and is not added to the chat transcript.", "dim")],
            [("", "normal")],
        ]
        participant_id = self._active_participant_id()
        updates = [
            update
            for update in self.progress_updates.values()
            if self.ui_preferences.show_progress
            or update.participant_id != participant_id
        ]
        if not updates:
            lines.append([("No active or recent progress updates.", "dim")])
            if not self.ui_preferences.show_progress:
                lines.append(
                    [("Your own progress updates are hidden. Use /progress on to show them.", "dim")]
                )
            return lines
        for update in sorted(
            updates,
            key=lambda item: item.updated_at,
            reverse=True,
        ):
            label = PROGRESS_LABELS.get(update.phase, "Working")
            detail = f" · {update.detail}" if update.detail else ""
            participant = update.participant_id[:6]
            lines.append(
                [
                    (
                        f"{update.player_name} · client {participant} · {label}{detail}",
                        "ai",
                    )
                ]
            )
        return lines

    def _data_panel_lines(self, width: int) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = [
            [("PRIVATE KNOWLEDGE", "player")],
            [
                (
                    "Files stay on this device. Only relevant excerpts are sent when you chat.",
                    "dim",
                )
            ],
            [("", "normal")],
        ]
        if not self.account_access_token:
            return lines + [
                [("Sign in with /account login to manage knowledge.", "action")],
                [("Account tokens are kept in memory and are never saved locally.", "dim")],
            ]
        lines.append(
            [
                (
                    f"Signed in as {self.account_display_name or 'your account'}.",
                    "ai",
                )
            ]
        )
        lines.append(
            [
                (
                    "Use /knowledge add FILE [CATEGORY] to index a UTF-8 text file.",
                    "dim",
                )
            ]
        )
        if not self.knowledge_sources:
            lines.append([("No indexed sources. Use /knowledge list to refresh.", "dim")])
            return lines
        lines.append([("", "normal")])
        for source in self.knowledge_sources:
            source_id = source.get("id", "?")
            title = str(source.get("title", "Untitled"))
            category = str(source.get("category", "general"))
            url = str(source.get("source_url", ""))
            lines.append([(f"{source_id:>4}  {title}  [{category}]", "normal")])
            lines.extend(
                [(line, "dim")] for line in self._wrapped_lines(url, max(10, width - 2))
            )
        return lines

    def _review_panel_lines(self) -> list[list[tuple[str, str]]]:
        if not self.coding_state.job_patch:
            return [
                [("PATCH REVIEW", "player")],
                [("", "normal")],
                [("No agent patch is waiting for review.", "dim")],
                [("When an agent finishes, inspect its patch here.", "dim")],
            ]
        lines = [
            [("REVIEW AGENT PATCH", "player")],
            [
                (
                    "Changes are isolated until you explicitly apply them.",
                    "dim",
                )
            ],
            [("Ctrl+A apply · Ctrl+D discard · /diff workspace", "dim")],
            [("", "normal")],
        ]
        lines.extend(
            [
                (
                    line,
                    tui.diff_style(line),
                )
            ]
            for line in self.coding_state.job_patch.splitlines()
        )
        lines.extend(
            [
                [("", "normal")],
                [("Ctrl+A Apply patch    Ctrl+D Discard patch    Esc Back", "player")],
            ]
        )
        return lines

    def _coding_panel_lines(self, width: int) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []
        lines.append(
            [
                ("Agent: ", "dim"),
                (self.coding_state.provider, "ai"),
                ("  ·  ", "dim"),
                (self.coding_state.job_status, "player"),
            ]
        )
        available = max(1, width)
        if self.coding_state.plan:
            lines.append([("TASK BRIEF", "player")])
            lines.extend(
                [("  " + line, "normal")]
                for line in self._wrapped_lines(self.coding_state.plan, available)
            )
        else:
            lines.extend([
                [("", "normal")],
                [("Build something, one step at a time", "player")],
                [("", "normal")],
                [("1  Describe   /code <what you want to change>", "normal")],
                [("2  Run        /run sends your brief to the agent", "normal")],
                [("3  Review     F6 shows the patch before you apply", "normal")],
                [("", "normal")],
                [("Choose a project with /workspace and an agent with /agent use.", "dim")],
            ])
        if self.coding_state.skills:
            lines.append([("Skills: " + ", ".join(self.coding_state.skills), "dim")])
        if self.coding_state.coding_history:
            lines.append([("CODING-MODE HISTORY", "player")])
            for turn in self.coding_state.coding_history[-10:]:
                role = turn["role"].upper()
                lines.extend(
                    [(f"{role}: {line}", "dim" if role == "AGENT" else "normal")]
                    for line in self._wrapped_lines(turn["content"], available)
                )
        if self.coding_state.job_command:
            lines.append([("Command: " + self.coding_state.job_command, "dim")])
        output_lines = (
            self.agent_output_lines
            if self.agent_process is not None
            else self.coding_state.job_output.splitlines()
        )
        if output_lines:
            lines.append([("AGENT OUTPUT", "player")])
            lines.extend(
                [("  " + line, "dim")]
                for line in output_lines[-200:]
                for line in self._wrapped_lines(line, available)
            )
        if self.coding_state.job_patch:
            heading = (
                "APPLIED AGENT DIFF"
                if self.coding_state.job_status == "applied"
                else "AGENT DIFF — review with /diff, then /apply or /discard"
            )
            lines.append([(heading, "player")])
            patch_lines = self.coding_state.job_patch.splitlines()
            lines.extend(
                [(line, tui.diff_style(line))]
                for line in patch_lines[-500:]
            )
        if self.coding_state.proposed_command:
            lines.append([("COMMAND PROPOSAL", "player")])
            lines.append([("Directory: " + self.coding_state.command_cwd, "dim")])
            lines.extend(
                [("  " + line, "normal")]
                for line in self._wrapped_lines(
                    self.coding_state.proposed_command, available
                )
            )
        if self.coding_state.command_output:
            lines.append([("COMMAND OUTPUT", "player")])
            lines.extend(
                [("  " + line, "dim")]
                for line in self.coding_state.command_output.splitlines()[-100:]
                for line in self._wrapped_lines(line, available)
            )
        if self.command_confirmation:
            lines.append([("Type /approve or /reject to continue.", "action")])
        return lines

    def _wrapped_lines(self, text: str, width: int) -> list[str]:
        if not text:
            return ["Start a conversation by typing below."]
        lines = []
        for source_line in text.splitlines() or [""]:
            wrapped = textwrap.wrap(
                source_line,
                width=max(1, width),
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
            lines.extend(wrapped or [""])
        return lines

    def _transcript_lines(
        self,
        session: ChatSession | ChatProfile,
        width: int,
    ) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []
        for message in parse_chat_log(session.chat_log):
            is_ai = message.speaker.casefold() in {"ai", "assistant"}
            name = "Super Bot" if is_ai else message.speaker
            header_style = "ai" if is_ai else "player"
            timestamp = format_timestamp(message.timestamp) if message.timestamp else ""
            lines.append([("● " + name, header_style), ("  " + timestamp, "dim")])
            in_code = False
            for source_line in message.text.splitlines() or [""]:
                if source_line.strip().startswith("```"):
                    in_code = not in_code
                    if in_code:
                        lines.append([("│ " + (source_line.strip()[3:] or "code"), "dim")])
                    continue
                segments = [(source_line, "code")] if in_code else style_message_text(source_line)
                for wrapped in self._wrap_rich_segments(segments, max(1, width - 2)):
                    lines.append([("│ " if in_code else "  ", "dim"), *wrapped])
            lines.append([("", "normal")])
        return lines

    def _wrap_rich_segments(
        self,
        segments: list[tuple[str, bool | str]],
        width: int,
    ) -> list[list[tuple[str, str]]]:
        width = max(1, width)
        lines: list[list[tuple[str, str]]] = [[]]
        line_length = 0
        for text, style in segments:
            style_name = "action" if style is True else "normal" if style is False else style
            for token in re.findall(r"\n|[^\S\n]+|[^\s]+", text):
                if token == "\n":
                    lines.append([])
                    line_length = 0
                    continue
                token = tui.clean(token)
                token_width = tui.width(token)
                if line_length and line_length + token_width > width:
                    lines.append([])
                    line_length = 0
                    if token.isspace():
                        continue
                while tui.width(token) > width:
                    chunk = tui.clip(token, width)
                    # A two-cell glyph cannot fit a one-cell panel.
                    if not chunk:
                        chunk, token = "?", token[1:]
                    else:
                        token = token[len(chunk):]
                    lines[-1].append((chunk, style_name))
                    lines.append([])
                    line_length = 0
                if token:
                    lines[-1].append((token, style_name))
                    line_length += tui.width(token)
        return lines

    def _draw_rich_line(
        self,
        y: int,
        x: int,
        segments: list[tuple[str, str]],
        width: int,
    ) -> None:
        attributes = {
            "player": curses.A_BOLD | self._color(1),
            "ai": curses.A_BOLD | self._color(2),
            "action": curses.A_DIM | getattr(curses, "A_ITALIC", 0),
            "dim": curses.A_DIM,
            "normal": 0,
            "success": self._color(2),
            "error": self._color(4),
            "code": self._color(6),
        }
        column = x
        for text, style in segments:
            if column >= x + width:
                break
            visible_text = tui.clip(text, x + width - column)
            self._put(y, column, visible_text, attributes[style])
            column += tui.width(visible_text)

    def _put(self, y: int, x: int, text: str, attributes: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width:
            try:
                self.screen.addstr(y, x, tui.clip(text, width - x - 1), attributes)
            except curses.error:
                pass

    @staticmethod
    def _color(pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    def _select_tab(self, tab: str) -> None:
        if tab not in TABS:
            return
        self.active_tab = tab
        self.view_mode = {
            "Chat": "chat",
            "Data": "data",
            "Activity": "chat",
            "Coding": "coding",
            "Review": "coding",
        }[tab]
        self.scroll_offset = 0
        self.coding_scroll_offset = 10**9 if tab == "Review" else 0
        self.action_menu_visible = False

    def _cycle_tab(self, direction: int) -> None:
        index = TABS.index(self.active_tab)
        self._select_tab(TABS[(index + direction) % len(TABS)])

    def _toggle_split(self) -> None:
        split_enabled = self.view_mode != "split"
        self._select_tab("Chat")
        self.view_mode = "split" if split_enabled else "chat"
        self.status = (
            "Chat and coding split view enabled"
            if self.view_mode == "split"
            else "Chat-focused view enabled"
        )

    def _activate_action_menu_item(self) -> None:
        items = self._filtered_actions()
        if not items:
            return
        selected = items[min(self.action_menu_index, len(items) - 1)][0]
        self.action_menu_visible = False
        if selected in TABS:
            self._select_tab(selected)
        elif selected == "Split":
            self._toggle_split()
        elif selected == "Help":
            self.help_visible = True
            self.scroll_offset = 10**9
        elif selected.startswith("/open "):
            self._open_saved_conversation(int(selected.split()[1]) - 1)
        else:
            # Fill the composer so actions are visible before they are run.
            self.input_text = "/new" if selected == "New session" else selected
            self.input_cursor = len(self.input_text)
            self.status = "Press Enter to run, or edit the command first"

    def _open_saved_conversation(self, index: int) -> None:
        if self.future is not None or self.pending_message:
            self.status = "Wait for the current reply before changing conversations"
            return
        self.handle_command(f"/open {index + 1}")
        self._select_tab("Chat")

    def _handle_mouse(self) -> None:
        if not self.mouse_enabled:
            return
        try:
            _, x, y, _, button_state = curses.getmouse()
        except curses.error:
            return
        if self.action_menu_visible:
            return
        if button_state & getattr(curses, "BUTTON4_PRESSED", 0):
            self._scroll_view(3)
            return
        if button_state & getattr(curses, "BUTTON5_PRESSED", 0):
            self._scroll_view(-3)
            return
        if not button_state & curses.BUTTON1_CLICKED:
            return
        if y == 2:
            for tab, start, end in self.tab_hit_targets:
                if start <= x < end:
                    self._select_tab(tab)
                    return
        if 1 <= x < 28:
            for row, index in self.session_hit_targets:
                if row == y:
                    if index < 0:
                        self.input_text = "/new"
                        self.input_cursor = len(self.input_text)
                        self.status = "Press Enter to start a new conversation"
                    else:
                        self._open_saved_conversation(index)
                    return

    def _scroll_view(self, amount: int) -> None:
        name = "coding_scroll_offset" if self.view_mode == "coding" and not self.help_visible else "scroll_offset"
        setattr(self, name, max(0, getattr(self, name) + amount))

    def handle_key(self, key: int | str) -> None:
        if key == "\x03":
            self.closed = True
            return
        if key == curses.KEY_RESIZE:
            return
        if key == "\x10":
            if getattr(self, "action_menu_visible", False):
                self.action_menu_visible = False
            else:
                self._open_actions()
            return
        if key == "\x0f":
            self._open_actions("/open")
            return
        if getattr(self, "action_menu_visible", False):
            items = self._filtered_actions()
            if key == "\x1b":
                self.action_menu_visible = False
            elif key == curses.KEY_UP:
                self.action_menu_index = (self.action_menu_index - 1) % max(1, len(items))
            elif key in (curses.KEY_DOWN, "\t"):
                self.action_menu_index = (self.action_menu_index + 1) % max(1, len(items))
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self._activate_action_menu_item()
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                self.action_query = self.action_query[:-1]
                self.action_menu_index = 0
            elif key == "\x15":
                self.action_query = ""
                self.action_menu_index = 0
            elif isinstance(key, str) and key.isprintable() and len(self.action_query) < 100:
                self.action_query += key
                self.action_menu_index = 0
            return
        if key == curses.KEY_F0 + 1:
            self.nav_help_visible = not self.nav_help_visible
            self.ui_preferences.show_navigation_help = self.nav_help_visible
            self._save_ui_preferences()
            return
        if key == curses.KEY_MOUSE:
            self._handle_mouse()
            return
        if isinstance(key, int) and curses.KEY_F0 + 2 <= key <= curses.KEY_F0 + 6:
            self.help_visible = False
            self._select_tab(TABS[key - curses.KEY_F0 - 2])
            return
        if key == curses.KEY_F0 + 7:
            self._toggle_split()
            return
        text = getattr(self, "input_text", "")
        if key == "\t":
            if text.startswith("/") and not self.help_visible:
                self._open_actions(text.strip())
            else:
                self._cycle_tab(1)
            return
        if key == getattr(curses, "KEY_BTAB", -1):
            self._cycle_tab(-1)
            return
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT) and not text and not getattr(self, "help_visible", False):
            self._cycle_tab(1 if key == curses.KEY_RIGHT else -1)
            return
        if self.help_visible:
            if key == "\x1b":
                self.help_visible = False
                self.scroll_offset = 0
            elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                self.scroll_offset += 1 if key == curses.KEY_UP else max(1, self.screen.getmaxyx()[0] - 11)
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                self.scroll_offset = max(0, self.scroll_offset - (1 if key == curses.KEY_DOWN else max(1, self.screen.getmaxyx()[0] - 11)))
            elif key == curses.KEY_HOME:
                self.scroll_offset = 10**9
            elif key == curses.KEY_END:
                self.scroll_offset = 0
            return
        cursor = min(getattr(self, "input_cursor", len(text)), len(text))
        if key in ("\n", "\r", curses.KEY_ENTER):
            if self.future is not None and self.pending_kind != "message_retry":
                self.status = "Waiting for the current reply · your draft is kept"
            else:
                self.submit_input()
                self.input_cursor = len(self.input_text)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            if text:
                self.input_cursor = max(0, min(len(text), cursor + (1 if key == curses.KEY_RIGHT else -1)))
            else:
                self._cycle_tab(1 if key == curses.KEY_RIGHT else -1)
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            if cursor:
                self.input_text = text[:cursor - 1] + text[cursor:]
                self.input_cursor = cursor - 1
        elif key == curses.KEY_DC:
            self.input_text = text[:cursor] + text[cursor + 1:]
            self.input_cursor = cursor
        elif key == "\x15":
            self.input_text = ""
            self.input_cursor = 0
        elif key == "\x17":
            before = re.sub(r"\S+\s*$", "", text[:cursor])
            self.input_text = before + text[cursor:]
            self.input_cursor = len(before)
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            amount = max(1, self.screen.getmaxyx()[0] - 11) if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            self._scroll_view(amount if key in (curses.KEY_UP, curses.KEY_PPAGE) else -amount)
        elif key == curses.KEY_HOME:
            if text:
                self.input_cursor = 0
            else:
                self._scroll_view(10**9)
        elif key in (curses.KEY_END, "\x05"):
            if text:
                self.input_cursor = len(text)
            else:
                self._scroll_view(-10**9)
        elif key == "\x01":
            if self.active_tab == "Review":
                self._apply_agent_patch()
            else:
                self.input_cursor = 0
        elif key == "\x04" and self.active_tab == "Review":
            self._discard_agent_patch()
        elif key == "\x1b" and self.active_tab != "Chat":
            self._select_tab("Chat")
        elif isinstance(key, str) and key.isprintable() and len(text) < 8000:
            self.input_text = text[:cursor] + key + text[cursor:]
            self.input_cursor = cursor + len(key)

    def submit_input(self) -> None:
        value = self.input_text.strip()
        if not value:
            return
        if self.retry_due is not None or self.pending_message:
            if value.casefold() == "/retry cancel":
                self.input_text = ""
                self._cancel_message_retry()
            else:
                self.status = "Error: cancel the pending retry with /retry cancel first"
            return
        if value.startswith("/"):
            self.input_text = ""
            self.handle_command(value)
            return
        if self.coding_only_mode:
            if len(value) > 4000:
                self.status = "Error: coding tasks must be at most 4000 characters"
                return
            self.input_text = ""
            self._submit_code_only_task(value)
            return
        session = self.active_session
        if session is None:
            self.status = "Error: create a session with /new first"
            return
        if len(value) > 1000:
            self.status = "Error: messages must be at most 1000 characters"
            return
        self.input_text = ""
        self._send_chat_message(value, session.server_session_id)

    def _submit_code_only_task(self, task: str) -> bool:
        if not self.coding_only_mode:
            self.status = "Error: code-only mode is not enabled"
            return False
        if self.agent_process is not None or self.coding_future is not None:
            self.code_only_queue.append(task)
            self.status = (
                f"Queued code-only task ({len(self.code_only_queue)} waiting); "
                "it will run after the current agent task."
            )
            self._save_coding_state()
            return True
        if self.coding_state.job_patch:
            self.code_only_queue.append(task)
            self.status = (
                "Queued code-only task. Review the current patch with /apply or "
                "/discard before the next agent task starts."
            )
            self._save_coding_state()
            return True
        return self._start_code_only_task(task)

    def _start_code_only_task(self, task: str) -> bool:
        if not self.coding_state.command_template:
            self.status = (
                "Error: configure a coding CLI first with /agent configure"
            )
            return False
        try:
            skills = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return False

        self._add_coding_turn("user", task)
        existing_brief = self.coding_state.plan.strip()
        prompt = (
            "Work directly on the user's coding request. The phone model is unavailable; "
            "The current directory is a disposable project copy; resolve project paths relative to it. "
            "Do not expect further instructions or review from the AI assistant. Treat the "
            "following as the user's task, inspect relevant files, make the "
            "requested changes, and run focused validation.\n\n"
            f"USER TASK:\n{task}\n\n"
            f"EXISTING CODING BRIEF (if relevant):\n"
            f"{existing_brief or '(No existing brief.)'}\n\n"
            f"CODING-MODE HISTORY:\n"
            f"{coding_tools.coding_history_text(self.coding_state.coding_history)}\n\n"
            f"SELECTED SKILLS:\n{skills}\n\n"
            "Preserve unrelated work and user changes. Do not read or reveal "
            ".env files, credentials, or secrets. Do not commit changes. Summarize "
            "changed files and test results."
        )
        self.coding_state.plan_request = task
        self.coding_state.plan_workspace = str(self.workspace)
        if not self._launch_agent_prompt(prompt):
            return False
        self.coding_state.plan_status = "submitted"
        self._save_coding_state()
        return True

    def _start_next_code_only_task(self) -> None:
        if (
            not self.coding_only_mode
            or not self.code_only_queue
            or self.agent_process is not None
            or self.coding_future is not None
            or (
                self.coding_state.job_patch
                and self.coding_state.job_status != "applied"
            )
        ):
            return
        task = self.code_only_queue[0]
        if self._start_code_only_task(task):
            self.code_only_queue.pop(0)

    def _enable_code_only_mode(self, error: Exception, task: str = "") -> bool:
        if not self._confirm_code_only_mode(error):
            self.status = "Phone model unavailable; code-only mode declined."
            return False
        self.coding_only_mode = True
        self.status = (
            "Code-only mode enabled. Messages go directly to the coding agent; "
            "AI chat and planning are disabled."
        )
        if task and not self._submit_code_only_task(task):
            return False
        return True

    def _confirm_code_only_mode(self, error: Exception) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = (
            f"Phone model failed ({error}). Continue in code-only mode? [y/N] "
        )
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, " PHONE MODEL UNAVAILABLE ", curses.A_BOLD | self._color(4))
            self._put(height // 2 - 1, 2, prompt[: max(1, width - 4)])
            detail = (
                "Messages will go directly to your coding agent; AI chat "
                "and planning won't be available."
            )
            self._put(height // 2, 2, detail[: max(1, width - 4)])
            self.screen.move(height // 2 + 1, 2)
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _send_chat_message(self, message: str, session_id: int) -> None:
        session = next(
            (
                saved_session
                for saved_session in self.sessions
                if saved_session.server_session_id == session_id
            ),
            None,
        )
        if session is None or not session.access_token:
            self.status = (
                f"Error: log in to server session {session_id} with /login {session_id}"
            )
            return
        try:
            skill_text = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return
        coding_context = (
            "Coding-mode discussion:\n"
            f"{coding_tools.coding_history_text(self.coding_state.coding_history)}\n\n"
            f"Selected coding skills:\n{skill_text}"
        )[-24_000:]
        self.pending_message = message
        self.pending_message_session_id = session_id
        self.pending_message_chat_context = session.chat_log[-12_000:]
        self.pending_message_coding_context = coding_context
        self.pending_message_can_start_coding = (
            self.coding_future is None and not self.coding_state.job_patch
        )
        self.message_progress = (
            "Sending message…" if self.ui_preferences.show_progress else ""
        )
        self.retry_delay_seconds = 5
        self.status = "Waiting for phone model/server reply…"
        self.pending_kind = "message"
        self.future = self.executor.submit(
            send_message,
            session.server_url or self.base_url,
            session_id,
            message,
            session.access_token,
            coding_context,
            self.ui_preferences.show_progress,
            session.learn_from_chats,
        )

    def _confirm_coding_mode(self) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = "The phone model thinks this is a coding request. Start coding mode? [y/N] "
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, " CODING REQUEST DETECTED ", curses.A_BOLD | self._color(3))
            self._put(height // 2, 2, prompt[: max(1, width - 4)])
            self.screen.move(height // 2, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _confirm_agent_run(self) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = "Brief ready. Start the coding agent now? [y/N] "
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, " CODING BRIEF READY ", curses.A_BOLD | self._color(3))
            self._put(height // 2, 2, prompt[: max(1, width - 4)])
            self.screen.move(height // 2, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _confirm_agent_install(self, label: str, plan: list[list[str]]) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = "Run these commands on this computer? [y/N] "
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, f" INSTALL {label.upper()} ", curses.A_BOLD | self._color(3))
            for row, step in enumerate(plan[: max(0, height - 6)], start=2):
                self._put(row, 2, f"$ {shlex.join(step)}"[: max(1, width - 4)])
            prompt_row = min(height - 2, 3 + len(plan))
            self._put(prompt_row, 2, prompt[: max(1, width - 4)])
            self.screen.move(prompt_row, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def handle_command(self, command: str) -> None:
        parts = command.split()
        action = parts[0].lower()
        argument = command.partition(" ")[2].strip()
        if self.pending_message and action != "/retry":
            self.status = "Error: cancel the pending message retry before other commands"
            return
        if action == "/new":
            self.create_session()
        elif action == "/connect":
            self._connect_to_server()
        elif action == "/server":
            self._handle_server_command(argument)
        elif action == "/list":
            self.status = "Saved sessions: " + (
                ", ".join(
                    f"{index + 1}: {session.player_name}"
                    for index, session in enumerate(self.sessions)
                )
                or "none"
            )
        elif action == "/open" and len(parts) == 1:
            if not self.sessions:
                self.create_session()
            elif self.active_session is None:
                self.active_index = len(self.sessions) - 1
                self.scroll_offset = 0
                self._start_session_events()
                self.status = f"Opened saved session {self.active_index + 1}"
            else:
                self.status = f"Session {self.active_index + 1} is already open"
        elif action == "/open" and len(parts) == 2 and parts[1].isdigit():
            index = int(parts[1]) - 1
            if 0 <= index < len(self.sessions):
                self.active_index = index
                self.scroll_offset = 0
                self._start_session_events()
                self.status = f"Opened saved session {index + 1}"
            else:
                self.status = "Error: no saved session with that number"
        elif action == "/login" and len(parts) <= 2:
            session_text = parts[1] if len(parts) == 2 else self._prompt("Session ID")
            if not session_text.isdigit() or int(session_text) < 1:
                self.status = "Error: session ID must be a positive integer"
                return
            session_id = int(session_text)
            password = self._prompt("Session password (leave blank if none)", secret=True)
            self.status = f"Authenticating to server session {session_id}…"
            self.pending_kind = "login"
            self.pending_session_data = (str(session_id), "")
            self.future = self.executor.submit(
                login_remote_session,
                self.base_url,
                session_id,
                password,
            )
        elif action == "/style":
            self._handle_style_command(command.partition(" ")[2].strip())
        elif action == "/chatskill":
            self._handle_chat_skill_command(command.partition(" ")[2].strip())
        elif action == "/profile":
            self._handle_profile_command(command.partition(" ")[2].strip())
        elif action == "/setup":
            self.setup_state.completed = False
            self._run_setup_wizard()
        elif action == "/character":
            self._handle_character_command(argument)
        elif action == "/account":
            self._handle_account_command(argument)
        elif action == "/knowledge":
            self._handle_knowledge_command(argument)
        elif action == "/export":
            self._handle_export_command(argument)
        elif action == "/log":
            self._handle_log_command(command.partition(" ")[2].strip())
        elif action == "/retry" and len(parts) == 2 and parts[1].casefold() == "cancel":
            self._cancel_message_retry()
        elif action == "/progress" and len(parts) == 2 and parts[1].casefold() in {"on", "off"}:
            self.ui_preferences.show_progress = parts[1].casefold() == "on"
            if not self.ui_preferences.show_progress:
                self.message_progress = ""
            self._save_ui_preferences()
            self.status = (
                "Your message progress is visible"
                if self.ui_preferences.show_progress
                else "Your message progress is hidden; the session host can still see it"
            )
        elif action == "/progress" and len(parts) == 1:
            self.status = (
                "Your message progress is "
                f"{'visible' if self.ui_preferences.show_progress else 'hidden'}"
            )
        elif action == "/learn" and len(parts) == 2 and parts[1].casefold() in {"on", "off"}:
            session = self.active_session
            if session is None:
                self.status = "Error: create or open a session before changing chat learning"
                return
            previous_preference = session.learn_from_chats
            session.learn_from_chats = parts[1].casefold() == "on"
            try:
                save_sessions(self.data_file, self.sessions)
            except OSError as exc:
                session.learn_from_chats = previous_preference
                self.status = f"Error saving chat-learning preference: {exc}"
                return
            self.status = (
                "Future messages from this client may be stored locally for training "
                "if the server operator enabled SUPER_BOT_COLLECT_CHAT_TRAINING=1. "
                "Do not opt in private or sensitive content."
                if session.learn_from_chats
                else "Future messages from this client will not be added to the training queue"
            )
        elif action == "/learn" and len(parts) == 1:
            session = self.active_session
            self.status = (
                "Chat learning is "
                f"{'on' if session.learn_from_chats else 'off'} for this session"
                if session is not None
                else "Error: create or open a session first"
            )
        elif action == "/code" and argument:
            self._start_plan(argument)
        elif action == "/refine" and argument:
            self._refine_plan(argument)
        elif action == "/plan":
            self._select_tab("Coding")
            self.coding_scroll_offset = 0
            self.status = (
                "Review the phone model's brief; use /refine <feedback> or /run."
                if self.coding_state.plan
                else "No plan yet. Use /code <request>."
            )
        elif action == "/run":
            self._run_agent()
        elif action == "/coding-mode":
            self._set_coding_mode(argument)
        elif action == "/coding-only" and argument.casefold() == "off":
            self.coding_only_mode = False
            self.status = "Code-only mode disabled"
        elif action == "/coding-only":
            self.status = (
                "Code-only mode is "
                f"{'enabled' if self.coding_only_mode else 'disabled'}"
            )
        elif action == "/status":
            self._select_tab("Coding")
            self.status = (
                f"Plan: {self.coding_state.plan_status}; "
                f"agent: {self.coding_state.job_status}"
            )
        elif action == "/diff" and argument.casefold() == "workspace":
            if self._start_coding_request("diff", coding_tools.git_diff, self.workspace):
                self.status = "Loading current workspace diff…"
        elif action == "/diff":
            if self.coding_state.job_patch:
                self.coding_state.job_output = self.coding_state.job_patch
                self._select_tab("Review")
                self.status = "Agent patch loaded; inspect it before /apply or /discard."
            elif self._start_coding_request("diff", coding_tools.git_diff, self.workspace):
                self.status = "Loading workspace diff…"
        elif action == "/apply":
            self._apply_agent_patch()
        elif action == "/discard":
            self._discard_agent_patch()
        elif action == "/cancel":
            self._cancel_agent()
        elif action == "/suggest":
            self._suggest_task()
        elif action == "/workspace":
            self._select_workspace(argument)
        elif action == "/view" and len(parts) == 2 and parts[1].casefold() in {
            "chat",
            "data",
            "activity",
            "coding",
            "review",
            "split",
        }:
            view = parts[1].casefold()
            if view == "split":
                self._select_tab("Chat")
                self.view_mode = "split"
            else:
                self._select_tab(view.title())
            self.status = f"Changed view to {view}"
        elif action == "/agent":
            self._handle_agent_command(parts[1:])
        elif action == "/skill":
            self._handle_skill_command(command.partition(" ")[2].strip())
        elif action == "/command" and argument:
            if self.coding_only_mode:
                self.status = (
                    "Error: command suggestions require the phone model; use the coding "
                    "agent directly with a normal message instead."
                )
                return
            self.coding_state.command_request = argument
            self.coding_state.proposed_command = ""
            self.coding_state.command_output = ""
            self.coding_state.command_cwd = str(self.workspace)
            if self._start_coding_request(
                "command", coding_tools.suggest_command, self.workspace, argument
            ):
                self.status = "Asking the phone model for a command proposal…"
        elif action == "/approve":
            self._approve_command()
        elif action == "/reject":
            self._reject_command()
        elif action == "/quit":
            self.closed = True
        elif action == "/help":
            self.help_visible = True
            self.scroll_offset = 10**9
        else:
            self.status = "Error: unknown command · Ctrl+P finds available actions"

    def _start_coding_request(self, kind: str, function: Any, *args: Any) -> bool:
        if self.coding_future is not None:
            self.status = "Error: another phone-model coding request is still running"
            return False
        self.coding_future_kind = kind
        self.coding_future = self.executor.submit(function, *args)
        return True

    def _start_plan(self, request: str) -> None:
        if self.coding_only_mode:
            self._submit_code_only_task(request)
            return
        if self.coding_future is not None:
            self.status = "Error: another phone-model coding request is still running"
            return
        if len(request) > 4000:
            self.status = "Error: coding request must be at most 4000 characters"
            return
        if self.coding_state.job_patch:
            self.status = "Review the pending agent patch with /apply or /discard first"
            return
        try:
            skills = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return
        self.coding_state.plan_request = request
        self.pending_model_fallback_task = request
        self.coding_state.plan = ""
        self.coding_state.plan_workspace = str(self.workspace)
        self.coding_state.plan_status = "planning"
        self._add_coding_turn("user", request)
        chat_context = getattr(self, "coding_request_chat_context", "")
        self.coding_request_chat_context = ""
        self.coding_future_kind = "plan"
        self.coding_future = self.executor.submit(
            coding_tools.make_plan,
            self.workspace,
            request,
            self.coding_state.coding_history[:-1],
            skills,
            chat_context=chat_context,
        )
        self.status = "Phone model is preparing a coding brief…"
        self._save_coding_state()

    def _refine_plan(self, feedback: str) -> None:
        if self.coding_only_mode:
            self._submit_code_only_task(
                f"Continue the existing coding task with this user feedback:\n{feedback}"
            )
            return
        if not self.coding_state.plan:
            self.status = "Error: create a plan first with /code <request>"
            return
        if self.coding_state.plan_workspace != str(self.workspace):
            self.status = "Error: this brief belongs to another workspace; create a new plan"
            return
        if self.coding_future is not None:
            self.status = "Error: another phone-model coding request is still running"
            return
        self._add_coding_turn("user", feedback)
        self.pending_model_fallback_task = (
            f"Continue this existing task:\n{self.coding_state.plan}\n\n"
            f"Apply the user's latest feedback:\n{feedback}"
        )
        self.coding_state.plan_status = "refining"
        self.coding_future_kind = "refine"
        self.coding_future = self.executor.submit(
            coding_tools.refine_plan,
            self.workspace,
            self.coding_state.plan,
            feedback,
            coding_history=self.coding_state.coding_history[:-1],
        )
        self.status = "Phone model is updating the brief…"
        self._save_coding_state()

    def _set_coding_mode(self, mode: str) -> None:
        if not mode:
            self.status = (
                f"Coding mode: {self.coding_state.coding_mode} "
                "(brief, confirm, auto)"
            )
            return
        if mode not in {"brief", "confirm", "auto"}:
            self.status = (
                "Use /coding-mode brief, /coding-mode confirm, or /coding-mode auto"
            )
            return
        self.coding_state.coding_mode = mode
        self.status = f"Coding mode set to {mode}"
        self._save_coding_state()

    def _suggest_task(self) -> None:
        if self.coding_future is not None:
            self.status = "Error: another phone-model coding request is still running"
            return
        if self.coding_state.job_patch:
            self.status = "Review the pending agent patch with /apply or /discard first"
            return
        try:
            skill_text = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return
        file_list_entries: list[str] = []
        ignored_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__"}
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = sorted(
                directory
                for directory in dirs
                if directory not in ignored_dirs and not directory.startswith(".")
            )
            for filename in sorted(files):
                file_list_entries.append(
                    str((Path(root) / filename).relative_to(self.workspace))
                )
                if len(file_list_entries) >= 300:
                    break
            if len(file_list_entries) >= 300:
                break
        file_list = "\n".join(file_list_entries)
        request = (
            "Review the workspace file list and coding-mode discussion. Suggest a "
            "few practical, bounded coding tasks, then present the best one as "
            "a proposed task brief. Do not modify files."
        )
        self.coding_state.plan_request = request
        self.coding_state.plan_workspace = str(self.workspace)
        self.coding_state.plan_status = "suggesting"
        self.coding_future_kind = "suggest"
        self.coding_future = self.executor.submit(
            coding_tools.make_plan,
            self.workspace,
            request,
            self.coding_state.coding_history,
            skill_text,
            workspace_context=f"Workspace file list:\n{file_list}",
        )
        self.status = "Phone model is reviewing the workspace for suggestions…"
        self._save_coding_state()

    def _handle_agent_command(self, args: list[str]) -> None:
        if not args or args[0] == "list":
            providers = coding_tools.discover_providers()
            self.coding_state.job_output = "\n".join(
                f"{provider['id']}: {provider['label']} — "
                f"{'installed' if provider['available'] else 'not found'}"
                for provider in providers
            )
            self.coding_state.job_output += (
                f"\ncustom: configured as {self.coding_state.command_template}"
                if self.coding_state.provider == "custom"
                else ""
            )
            self._select_tab("Coding")
            self.status = f"Selected agent: {self.coding_state.provider}"
            self._save_coding_state()
        elif args[0] == "use" and len(args) == 2:
            if self.agent_process is not None or self.coding_future_kind == "agent_start":
                self.status = "Error: wait for the active coding agent before switching CLI"
                return
            provider = args[1].lower()
            if provider not in coding_tools.PROVIDER_SPECS:
                self.status = (
                    "Error: use a detected provider or /agent use custom; "
                    "see /agent list"
                )
                return
            self.coding_state.provider = provider
            try:
                self.coding_state.command_template = coding_tools.provider_template(provider)
                self.status = (
                    f"Selected {provider}. Configure or inspect it with "
                    "/agent configure or /agent help."
                )
            except ValueError:
                self.coding_state.command_template = ""
                self.status = f"Selected {provider}; configure its CLI with /agent configure."
            if provider == "custom":
                template = self._prompt(
                    "Custom CLI command template ({prompt} required)"
                )
                try:
                    coding_tools.build_agent_argv(template, "PROMPT")
                except ValueError as exc:
                    self.status = f"Error: custom agent not configured: {exc}"
                    return
                self.coding_state.command_template = template
                self.status = "Selected and configured custom coding CLI"
            self._save_coding_state()
        elif args[0] == "configure":
            if self.agent_process is not None or self.coding_future_kind == "agent_start":
                self.status = "Error: wait for the active coding agent before reconfiguring it"
                return
            template = self._prompt("Agent command template ({prompt} required)")
            if not template:
                self.status = "Agent configuration cancelled"
                return
            try:
                coding_tools.build_agent_argv(template, "PROMPT")
            except ValueError as exc:
                self.status = f"Error: {exc}"
                return
            self.coding_state.command_template = template
            self.status = "Agent command template saved"
            self._save_coding_state()
        elif args[0] == "help":
            provider = coding_tools.PROVIDER_SPECS.get(self.coding_state.provider)
            if provider is None or not self.coding_state.command_template:
                self.status = "Error: select a supported agent first"
                return
            try:
                configured_args = shlex.split(self.coding_state.command_template)
            except ValueError as exc:
                self.status = f"Error: invalid agent command template: {exc}"
                return
            if not configured_args:
                self.status = "Error: configure the agent CLI first"
                return
            self._start_coding_request(
                "agent_help",
                coding_tools.provider_help,
                configured_args[0],
                self.workspace,
            )
            self.status = f"Loading {provider['label']} native help…"
        elif args[0] == "install" and len(args) == 2:
            name = args[1].lower()
            provider = coding_tools.PROVIDER_SPECS.get(name)
            if provider is None or not provider["website"]:
                self.status = "Error: see /agent list or configure a custom CLI"
                return
            if self.agent_process is not None or self.coding_future is not None:
                self.status = "Error: wait for the active coding task before installing"
                return
            try:
                plan = coding_tools.install_plan(name)
            except ValueError as exc:
                self.coding_state.job_output = (
                    f"{exc}\nOfficial installation page:\n{provider['website']}"
                )
                self._select_tab("Coding")
                self.status = f"Error: {exc}"
                return
            if not self._confirm_agent_install(str(provider["label"]), plan):
                self.status = "Installation cancelled"
                return
            self.coding_state.job_output = (
                f"Installing {provider['label']}…\n"
                + "\n".join(f"$ {shlex.join(step)}" for step in plan)
            )
            self._select_tab("Coding")
            if self._start_coding_request("agent_install", coding_tools.run_install, plan):
                self.status = f"Installing {provider['label']}… this can take a few minutes"
        else:
            self.status = (
                "Use /agent list, use NAME, configure, help, or install NAME"
            )

    def _select_workspace(self, path_text: str) -> None:
        if (
            self.coding_future is not None
            or self.agent_process is not None
            or self.coding_state.job_patch
            or self.command_confirmation
        ):
            self.status = (
                "Error: finish or discard the pending coding task before changing workspace"
            )
            return
        try:
            paths = shlex.split(path_text) if path_text else []
        except ValueError as exc:
            self.status = f"Error: invalid workspace path: {exc}"
            return
        if len(paths) > 1:
            self.status = 'Error: use quotes around workspace paths containing spaces'
            return
        selected_path = paths[0] if paths else self._prompt("Project root folder")
        if not selected_path:
            self.status = "Workspace selection cancelled"
            return
        candidate = Path(selected_path).expanduser().resolve()
        if not candidate.is_dir():
            self.status = f"Error: workspace is not a directory: {candidate}"
            return

        self.workspace = candidate
        self.skills = coding_tools.discover_skills(
            self.workspace,
            self.coding_state.custom_skills,
        )
        self.coding_state.workspace = str(self.workspace)
        self.status = f"Project root set to {self.workspace}"
        self._save_coding_state()

    def _save_chat_skill_library(self) -> bool:
        try:
            save_chat_skills(self.chat_skills_path, self.chat_skill_library)
        except OSError as exc:
            self.status = f"Error saving chat skills: {exc}"
            return False
        return True

    def _handle_chat_skill_command(self, argument: str) -> None:
        parts = argument.split(None, 2)
        action = parts[0].casefold() if parts else "list"
        session = self.active_session
        available = {**CHAT_SKILLS, **self.chat_skill_library}
        active = session.chat_skills if session is not None else {}
        if action == "list" or (action == "show" and len(parts) < 2):
            names = [
                f"{name}{'*' if name in active else ''}"
                f"{'' if name in CHAT_SKILLS else ' [yours]'}"
                for name in available
            ]
            self.status = (
                "Chat skills: " + ", ".join(names) + " (* active). /chatskill use|off NAME, "
                "clear, show NAME, create NAME TEXT, delete NAME"
            )
            return
        if action == "show":
            name = parts[1].casefold()
            if name not in available:
                self.status = f"Error: unknown chat skill; choose from {', '.join(available)}"
                return
            self.coding_state.job_output = f"CHAT SKILL: {name}\n\n{available[name]}"
            self._select_tab("Coding")
            self.status = f"Showing chat skill: {name}"
            return
        if action in {"create", "delete"} and len(parts) >= 2:
            name = parts[1].casefold()
            if not SKILL_NAME.fullmatch(name):
                self.status = "Error: skill name must be 1-30 letters, numbers, _ or -"
                return
            if name in CHAT_SKILLS:
                self.status = "Error: built-in chat skills cannot be changed"
                return
            if action == "delete":
                if name not in self.chat_skill_library:
                    self.status = f"Error: you have no chat skill named {name}"
                    return
                del self.chat_skill_library[name]
                if self._save_chat_skill_library():
                    self.status = (
                        f"Deleted chat skill {name}; sessions already using it keep it"
                    )
                return
            text = " ".join(parts[2].split()) if len(parts) == 3 else ""
            if not text:
                self.status = "Use /chatskill create NAME INSTRUCTIONS"
                return
            if len(text) > MAX_SKILL_TEXT_LENGTH:
                self.status = (
                    f"Error: skill instructions must be at most {MAX_SKILL_TEXT_LENGTH} characters"
                )
                return
            self.chat_skill_library[name] = text
            if self._save_chat_skill_library():
                self.status = f"Saved chat skill {name} on this computer. Pick it at /new."
            return
        if action == "clear" and len(parts) == 1:
            updated: dict[str, str] = {}
        elif action in {"use", "off"} and len(parts) == 2:
            name = parts[1].casefold()
            updated = dict(active)
            if action == "use":
                if name not in available:
                    self.status = f"Error: unknown chat skill; choose from {', '.join(available)}"
                    return
                updated[name] = available[name]
            else:
                updated.pop(name, None)
            if len(updated) > MAX_ACTIVE_CHAT_SKILLS:
                self.status = (
                    f"Error: at most {MAX_ACTIVE_CHAT_SKILLS} chat skills can be active; "
                    "turn one off first"
                )
                return
        else:
            self.status = (
                "Use /chatskill list, show NAME, use NAME, off NAME, clear, "
                "create NAME TEXT, or delete NAME"
            )
            return
        if session is None or not session.access_token:
            self.status = "Error: open a session with /new or /login first"
            return
        if self.future is not None:
            self.status = "Error: wait for the current request to finish"
            return
        self.status = "Sending chat skills to the server…"
        self.pending_kind = "chat_skill"
        self.future = self.executor.submit(
            set_session_skills,
            session.server_url or self.base_url,
            session.server_session_id,
            updated,
            session.access_token,
        )

    def _handle_style_command(self, argument: str) -> None:
        if not argument:
            self.status = (
                f"Styles: {', '.join(STYLE_PRESETS)}, or /style YOUR OWN WORDS; "
                "/style off clears it"
            )
            return
        session = self.active_session
        if session is None or not session.access_token:
            self.status = "Error: open a session with /new or /login first"
            return
        if self.future is not None:
            self.status = "Error: wait for the current request to finish"
            return
        try:
            style = resolve_style(argument)
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return
        self.status = "Updating reply style…"
        self.pending_kind = "style"
        self.future = self.executor.submit(
            set_session_style,
            session.server_url or self.base_url,
            session.server_session_id,
            style,
            session.access_token,
        )

    def _handle_profile_command(self, argument: str) -> None:
        try:
            args = shlex.split(argument)
        except ValueError as exc:
            self.status = f"Error: invalid profile command: {exc}"
            return
        if not args or args[0] == "list":
            selected = self.profile_store.selected
            self.status = "Profiles: " + (
                ", ".join(
                    f"{name}{' (selected)' if name == selected else ''}"
                    for name in sorted(self.profile_store.profiles)
                )
                or "none"
            )
        elif args[0] == "save" and len(args) >= 2:
            session = self.active_session
            if session is None:
                self.status = "Error: open a conversation before saving a profile"
                return
            name = " ".join(args[1:]).strip()
            if not name:
                self.status = "Error: profile name cannot be empty"
                return
            existing = self.profile_store.profiles.get(name)
            persona = session.persona
            if existing is not None and persona == self._profile_persona(existing):
                persona = existing.persona
            self.profile_store.profiles[name] = ChatProfile(
                player_name=session.player_name,
                persona=persona,
                chat_log=session.chat_log,
                style=session.style,
                chat_skills=dict(session.chat_skills),
                preferred_model=existing.preferred_model if existing else "",
                character_development_enabled=(
                    existing.character_development_enabled if existing else False
                ),
                character_notes=existing.character_notes if existing else "",
            )
            session.profile_name = name
            self.profile_store.selected = name
            self.status = f"Saved and selected profile: {name}"
            if self._save_profile_store() and hasattr(self, "data_file"):
                try:
                    save_sessions(self.data_file, self.sessions)
                except OSError as exc:
                    self.status = f"Error saving profile association: {exc}"
        elif args[0] == "use" and len(args) >= 2:
            name = " ".join(args[1:]).strip()
            if name not in self.profile_store.profiles:
                self.status = f"Error: no saved profile named {name}"
                return
            self.profile_store.selected = name
            self.status = f"Selected profile for new sessions: {name}"
            self._save_profile_store()
        elif args[0] == "clear" and len(args) == 1:
            self.profile_store.selected = ""
            self.status = "Cleared selected profile; /new will ask for details"
            self._save_profile_store()
        elif args[0] == "delete" and len(args) >= 2:
            name = " ".join(args[1:]).strip()
            if self.profile_store.profiles.pop(name, None) is None:
                self.status = f"Error: no saved profile named {name}"
                return
            if self.profile_store.selected == name:
                self.profile_store.selected = ""
            self.status = f"Deleted profile: {name}"
            self._save_profile_store()
        else:
            self.status = (
                "Use /profile list, save NAME, use NAME, clear, or delete NAME"
            )

    def _handle_character_command(self, argument: str) -> None:
        profile = self.profile_store.profiles.get(self.profile_store.selected)
        if profile is None:
            self.status = "Error: select or create a profile before configuring character notes"
            return
        action, _, notes = argument.partition(" ")
        action = action.casefold()
        if action in {"", "status"}:
            self.status = (
                "Character development is "
                f"{'on' if profile.character_development_enabled else 'off'} for "
                f"{self.profile_store.selected}."
            )
            return
        if action == "off" and not notes:
            profile.character_development_enabled = False
            profile.character_notes = ""
            if self._save_profile_store():
                self.status = "Character development notes disabled for future sessions."
            return
        if action == "on":
            cleaned = " ".join(notes.split())
            if not cleaned:
                self.status = "Use /character on NOTES, or /character off"
                return
            if len(cleaned) > MAX_CHARACTER_NOTES_LENGTH:
                self.status = (
                    f"Error: character notes must be at most "
                    f"{MAX_CHARACTER_NOTES_LENGTH} characters"
                )
                return
            profile.character_development_enabled = True
            profile.character_notes = cleaned
            if self._save_profile_store():
                self.status = (
                    "Saved local character notes. They will be included in new sessions "
                    "for this profile only."
                )
            return
        self.status = "Use /character status, /character on NOTES, or /character off"

    def _handle_account_command(self, argument: str) -> None:
        action = argument.casefold()
        if action in {"", "status"}:
            self.status = (
                f"Signed in as {self.account_display_name or 'your account'}."
                if self.account_access_token
                else "Not signed in. Use /account login to access private knowledge."
            )
            return
        if action == "login":
            if self.future is not None:
                self.status = "Error: wait for the current request to finish"
                return
            username = self._prompt("Account username", title="ACCOUNT LOGIN")
            password = self._prompt("Account password", secret=True, title="ACCOUNT LOGIN")
            if not username or not password:
                self.status = "Account login cancelled"
                return
            self.status = "Signing in to account…"
            self.pending_kind = "account_login"
            self.future = self.executor.submit(
                request_json,
                self.base_url,
                "/accounts/login",
                {"username": username, "password": password},
            )
            return
        if action == "logout":
            if not self.account_access_token:
                self.status = "Not signed in."
                return
            if self.future is not None:
                self.status = "Error: wait for the current request to finish"
                return
            self.status = "Signing out…"
            self.pending_kind = "account_logout"
            self.future = self.executor.submit(
                request_json,
                self.base_url,
                "/accounts/logout",
                {},
                self.account_access_token,
            )
            return
        self.status = "Use /account login, /account logout, or /account status"

    def _handle_knowledge_command(self, argument: str) -> None:
        if not self.account_access_token:
            self.status = "Error: sign in with /account login before managing knowledge"
            return
        if self.future is not None:
            self.status = "Error: wait for the current request to finish"
            return
        try:
            parts = shlex.split(argument)
        except ValueError as exc:
            self.status = f"Error: invalid knowledge command: {exc}"
            return
        action = parts[0].casefold() if parts else "list"
        if action == "list" and len(parts) == 1:
            self.status = "Loading private knowledge…"
            self.pending_kind = "knowledge_list"
            self.future = self.executor.submit(
                request_json,
                self.base_url,
                "/accounts/knowledge",
                None,
                self.account_access_token,
            )
            return
        if action == "delete" and len(parts) == 2 and parts[1].isdigit():
            self.status = "Removing knowledge source…"
            self.pending_kind = "knowledge_delete"
            self.future = self.executor.submit(
                request_json,
                self.base_url,
                "/accounts/knowledge/delete",
                {"source_id": int(parts[1])},
                self.account_access_token,
            )
            return
        if action == "add" and len(parts) in {2, 3}:
            path = Path(parts[1]).expanduser()
            if not path.is_file():
                self.status = f"Error: not a file: {path}"
                return
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.status = f"Error reading {path}: {exc}"
                return
            if len(content.encode("utf-8")) > 1_000_000:
                self.status = "Error: knowledge files must be at most 1 MiB."
                return
            self.status = "Indexing private knowledge…"
            self.pending_kind = "knowledge_add"
            self.future = self.executor.submit(
                request_json,
                self.base_url,
                "/accounts/knowledge/add",
                {
                    "source_url": path.resolve().as_uri(),
                    "title": path.stem,
                    "content": content,
                    "category": parts[2] if len(parts) == 3 else "general",
                },
                self.account_access_token,
            )
            return
        self.status = "Use /knowledge list, add FILE [CATEGORY], or delete ID"

    def _handle_log_command(self, argument: str) -> None:
        try:
            args = shlex.split(argument)
        except ValueError as exc:
            self.status = f"Error: invalid chat-log command: {exc}"
            return
        if args == ["clear"]:
            self.pending_initial_chat_log = None
            self.status = "Cleared staged starting chat log"
        elif len(args) == 3 and args[:2] == ["load", "session"]:
            if not args[2].isdigit():
                self.status = "Error: session number must be a positive integer"
                return
            index = int(args[2]) - 1
            if not 0 <= index < len(self.sessions):
                self.status = "Error: no saved session with that number"
                return
            session = self.sessions[index]
            self.pending_initial_chat_log = session.chat_log
            self.status = (
                f"Loaded chat log from session {index + 1}; it will seed the next /new"
            )
        elif len(args) >= 2 and args[0] == "load" and args[1] == "profile":
            name = " ".join(args[2:]).strip()
            profile = self.profile_store.profiles.get(name)
            if profile is None:
                self.status = f"Error: no saved profile named {name}"
                return
            self.pending_initial_chat_log = profile.chat_log
            self.status = (
                f"Loaded chat log from profile {name}; it will seed the next /new"
            )
        elif not args or args == ["status"]:
            if self.pending_initial_chat_log is None:
                self.status = "No staged starting chat log"
            else:
                self.status = (
                    f"Starting chat log staged ({len(self.pending_initial_chat_log)} "
                    "characters); use /new or /log clear"
                )
        else:
            self.status = (
                "Use /log load session N, /log load profile NAME, "
                "/log status, or /log clear"
            )

    def _handle_export_command(self, argument: str) -> None:
        try:
            paths = shlex.split(argument)
        except ValueError as exc:
            self.status = f"Error: invalid export path: {exc}"
            return
        if len(paths) > 1:
            self.status = "Error: export accepts at most one path"
            return
        session = self.active_session
        if session is None:
            self.status = "Error: open a conversation before exporting it"
            return
        if paths:
            destination = Path(paths[0]).expanduser()
        else:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = self.data_file.with_name(
                f"chat-{session.server_session_id}-{timestamp}.txt"
            )
        try:
            export_chat(destination, session.chat_log)
        except FileExistsError:
            self.status = f"Error: export already exists: {destination}"
        except OSError as exc:
            self.status = f"Error exporting chat: {exc}"
        else:
            self.status = f"Exported chat to {destination}"

    def _save_profile_store(self) -> bool:
        try:
            save_profiles(self.profiles_path, self.profile_store)
        except OSError as exc:
            self.status = f"Error saving profiles: {exc}"
            return False
        return True

    def _sync_profile_chat_log(self, session: ChatSession) -> bool:
        profile_name = getattr(session, "profile_name", "")
        profile_store = getattr(self, "profile_store", None)
        if profile_store is None:
            return True
        profile = profile_store.profiles.get(profile_name)
        if profile is None:
            return True
        profile.player_name = session.player_name
        profile.persona = session.persona
        profile.chat_log = session.chat_log
        return self._save_profile_store()

    def _add_coding_turn(self, role: str, content: str) -> None:
        self.coding_state.coding_history.append({"role": role, "content": content})
        self.coding_state.coding_history = self.coding_state.coding_history[
            -coding_tools.MAX_CODING_HISTORY_TURNS :
        ]
        while (
            len(coding_tools.coding_history_text(self.coding_state.coding_history))
            > coding_tools.MAX_CODING_HISTORY_CHARS
            and len(self.coding_state.coding_history) > 1
        ):
            self.coding_state.coding_history.pop(0)

    def _handle_skill_command(self, argument: str) -> None:
        try:
            args = shlex.split(argument)
        except ValueError as exc:
            self.status = f"Error: invalid skill command: {exc}"
            return
        if not args or args[0] == "list":
            self.coding_state.job_output = "\n".join(
                f"{name}{' (selected)' if name in self.coding_state.skills else ''}"
                f"{' [custom]' if name in self.coding_state.custom_skills else ''}"
                for name in sorted(self.skills)
            ) or "No skills found."
            self._select_tab("Coding")
            self.status = "Built-in and discovered skills shown in coding view"
        elif args[0] == "show" and len(args) == 2:
            if args[1] not in self.skills:
                self.status = f"Error: unknown skill '{args[1]}'; use /skill list"
                return
            self.coding_state.job_output = f"SKILL: {args[1]}\n\n{self.skills[args[1]]}"
            self._select_tab("Coding")
            self.status = f"Showing skill: {args[1]}"
        elif args[0] == "use" and len(args) == 2:
            if args[1] not in self.skills:
                self.status = f"Error: unknown skill '{args[1]}'; use /skill list"
                return
            if args[1] not in self.coding_state.skills:
                self.coding_state.skills.append(args[1])
            self.status = f"Selected skill for the phone model and agent: {args[1]}"
        elif args[0] == "clear":
            self.coding_state.skills.clear()
            self.status = "Cleared selected skills"
        elif args[0] in {"create", "edit"} and len(args) >= 3:
            name = args[1]
            instructions = " ".join(args[2:]).strip()
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}", name):
                self.status = "Error: skill name must use letters, numbers, _ or -"
                return
            if not instructions or len(instructions) > 20_000:
                self.status = "Error: skill instructions must be 1-20000 characters"
                return
            if args[0] == "create":
                if name in self.skills:
                    self.status = f"Error: skill '{name}' already exists"
                    return
            elif name not in self.coding_state.custom_skills:
                self.status = f"Error: '{name}' is not a user-created skill"
                return
            self.coding_state.custom_skills[name] = instructions
            self.skills = coding_tools.discover_skills(
                self.workspace,
                self.coding_state.custom_skills,
            )
            self.status = f"{'Created' if args[0] == 'create' else 'Updated'} custom skill: {name}"
        elif args[0] == "delete" and len(args) == 2:
            if args[1] not in self.coding_state.custom_skills:
                self.status = f"Error: '{args[1]}' is not a user-created skill"
                return
            del self.coding_state.custom_skills[args[1]]
            if args[1] in self.coding_state.skills:
                self.coding_state.skills.remove(args[1])
            self.skills = coding_tools.discover_skills(
                self.workspace,
                self.coding_state.custom_skills,
            )
            self.status = f"Deleted custom skill: {args[1]}"
        else:
            self.status = (
                "Use /skill list, show NAME, use NAME, clear, "
                "create NAME INSTRUCTIONS, edit NAME INSTRUCTIONS, "
                "or delete NAME"
            )
        self._save_coding_state()

    def _run_agent(self) -> None:
        if not self.coding_state.plan or self.coding_state.plan_status != "reviewing":
            self.status = "Error: review the phone model's brief before using /run"
            return
        if self.agent_process is not None:
            self.status = "Error: a coding agent is already running"
            return
        if self.coding_state.plan_workspace != str(self.workspace):
            self.status = (
                "Error: this brief was made for another workspace. "
                "Create a fresh brief with /code <request>."
            )
            return
        try:
            skills = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
            prompt = (
                "Implement this user-approved task in the current workspace.\n\n"
                "The current directory is a disposable project copy. Resolve project paths relative to it.\n\n"
                f"TASK BRIEF:\n{self.coding_state.plan}\n\n"
                f"SELECTED SKILLS:\n{skills}\n\n"
                "Apply these skills regardless of which coding CLI is selected. "
                "Inspect relevant files before editing. Preserve unrelated work "
                "and user changes. Do not read or reveal .env files, credentials, "
                "or secrets. Do not commit changes. Run focused validation and "
                "summarize changed files and test results."
            )
        except (OSError, ValueError) as exc:
            self.status = (
                f"Error: could not start coding agent: {exc}. "
                "Check its installation and /agent configure."
            )
            return
        if self._launch_agent_prompt(prompt):
            self.coding_state.plan_status = "submitted"
            self._save_coding_state()

    def _launch_agent_prompt(self, prompt: str) -> bool:
        if self.agent_process is not None or self.coding_future is not None:
            self.status = "Error: wait for the active coding task to finish"
            return False
        if not self.coding_state.command_template:
            self.status = "Configure an agent CLI first with /agent configure."
            return False
        self.coding_state.job_status = "preparing"
        self.coding_state.job_patch = ""
        self.coding_state.job_output = ""
        self.coding_state.job_command = self.coding_state.command_template
        self.coding_future_kind = "agent_start"
        self.coding_future = self.executor.submit(
            coding_tools.AgentProcess,
            self.coding_state.command_template,
            prompt,
            self.workspace,
        )
        self.status = "Preparing isolated agent workspace… Open Coding (F5) or Split (F7)."
        self._save_coding_state()
        return True

    def _cancel_agent(self) -> None:
        if self.coding_future is not None:
            if self.coding_future.cancel():
                self.coding_future = None
                self.coding_future_kind = ""
                self.coding_state.job_status = "cancelled"
                self.coding_state.plan_status = "cancelled"
                self.status = "Cancelled pending coding request"
                self._save_coding_state()
                return
            if self.coding_future_kind == "agent_start":
                self.status = "Agent workspace preparation is underway; wait for it to finish."
                return
            self.status = "Phone model request is already in progress and cannot be interrupted."
            return
        if self.agent_process is None:
            self.status = "No coding agent is running"
            return
        self.agent_process.cancel()
        self.coding_state.job_status = "cancelling"
        self.status = "Stopping coding agent…"
        self._save_coding_state()

    def _approve_command(self) -> None:
        if not self.command_confirmation or not self.coding_state.proposed_command:
            self.status = "Error: there is no pending command to approve"
            return
        if self.coding_future is not None:
            self.status = "Error: wait for the active coding request to finish"
            return
        if self.agent_process is not None:
            self.status = "Error: wait for the coding agent to finish before running commands"
            return
        try:
            argv = coding_tools.command_argv(self.coding_state.proposed_command)
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return
        self.pending_command_argv = argv
        self.command_confirmation = False
        self.coding_state.command_cwd = str(self.workspace)
        self.status = "Running approved command…"
        self._start_coding_request("run_command", coding_tools.run_command, argv, self.workspace)

    def _reject_command(self) -> None:
        if not self.command_confirmation:
            self.status = "Error: there is no pending command to reject"
            return
        self.command_confirmation = False
        self.coding_state.proposed_command = ""
        self.coding_state.command_request = ""
        self.status = "Rejected the AI's command proposal"
        self._save_coding_state()

    def _apply_agent_patch(self) -> None:
        if not self.coding_state.job_patch:
            self.status = "Error: there is no agent patch to apply"
            return
        if self.coding_future is not None:
            self.status = "Error: another coding task is still running"
            return
        self._start_coding_request(
            "apply_patch",
            coding_tools.apply_agent_patch,
            self.workspace,
            self.coding_state.job_patch,
        )
        self.status = "Checking patch against current files before applying…"

    def _discard_agent_patch(self) -> None:
        if not self.coding_state.job_patch:
            self.status = "There is no agent patch to discard"
            return
        self.coding_state.job_patch = ""
        self.coding_state.job_output = ""
        if self.coding_state.job_status == "applied":
            self.status = "Cleared the saved patch preview; applied workspace changes remain"
        else:
            self.coding_state.job_status = "discarded"
            self.coding_state.plan_status = "reviewing"
            self.status = "Discarded the isolated agent patch; workspace was not changed"
        self._save_coding_state()
        self._start_next_code_only_task()

    def _finish_coding_request(self) -> None:
        if self.coding_future is None or not self.coding_future.done():
            return
        future = self.coding_future
        kind = self.coding_future_kind
        self.coding_future = None
        self.coding_future_kind = ""
        try:
            result = future.result()
            if kind in {"plan", "refine", "suggest"}:
                if kind in {"plan", "refine"}:
                    self.pending_model_fallback_task = ""
                self.coding_state.plan = str(result)[: coding_tools.MAX_PLAN_LENGTH]
                self.coding_state.plan_status = "reviewing"
                if kind in {"plan", "refine"}:
                    self._add_coding_turn("assistant", self.coding_state.plan)
                self.status = (
                    "Phone-model brief ready. Open Coding (F5), refine with /refine, "
                    "or use /run when you're happy with it."
                )
                if kind == "plan" and self.coding_state.coding_mode == "auto":
                    self._run_agent()
                    return
                if (
                    kind == "plan"
                    and self.coding_state.coding_mode == "confirm"
                    and self._confirm_agent_run()
                ):
                    self._run_agent()
                    return
            elif kind == "command":
                argv = coding_tools.command_argv(str(result))
                self.coding_state.proposed_command = shlex.join(argv)
                self.coding_state.command_cwd = str(self.workspace)
                self.command_confirmation = True
                self.coding_state.command_output = ""
                self._select_tab("Coding")
                self.status = (
                    "The phone model proposed a command. Review it in coding view, "
                    "then use /approve or /reject."
                )
            elif kind == "run_command":
                returncode, output = result
                self.coding_state.command_output = (
                    f"Exit code: {returncode}\n{output}"
                ).strip()
                self.coding_state.proposed_command = ""
                self.coding_state.command_request = ""
                self.pending_command_argv = None
                self.status = (
                    f"Approved command finished with exit code {returncode}"
                )
            elif kind == "agent_help":
                returncode, output = result
                self.coding_state.job_output = (
                    f"CLI help (exit code {returncode}):\n{output}"
                )
                self._select_tab("Coding")
                self.status = f"{self.coding_state.provider} help loaded"
            elif kind == "agent_install":
                returncode, output = result
                self.coding_state.job_output = (
                    f"Install finished (exit code {returncode}):\n{output}"
                )
                self._select_tab("Coding")
                self.status = (
                    "Install finished. Run /agent list to confirm it was detected."
                    if returncode == 0
                    else "Install failed; see the output in the Coding tab"
                )
            elif kind == "agent_start":
                self.agent_process = result
                self.agent_output_lines = []
                self.coding_state.job_status = "running"
                self.coding_state.job_command = shlex.join(result.argv)
                self.status = (
                    f"{self.coding_state.provider} is coding in an isolated workspace. "
                    "Chat remains available; use /view coding to focus on its output."
                )
            elif kind == "apply_patch":
                self.coding_state.plan_status = "applied"
                self.coding_state.job_status = "applied"
                self._select_tab("Review")
                self.status = "Agent patch safely applied. Review your workspace with /diff."
                self._save_coding_state()
                self._start_next_code_only_task()
            elif kind == "diff":
                self.coding_state.job_output = str(result)
                self._select_tab("Coding")
                self.coding_scroll_offset = 0
                self.status = "Workspace diff loaded"
            self.coding_scroll_offset = 0
            self._save_coding_state()
        except (
            RuntimeError,
            ValueError,
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
        ) as exc:
            if kind in {"plan", "refine"}:
                fallback_task = self.pending_model_fallback_task
                self.pending_model_fallback_task = ""
                self.coding_state.plan_status = "failed"
                if self._enable_code_only_mode(exc, fallback_task):
                    return
            if kind in {"plan", "refine", "suggest"}:
                self.coding_state.plan_status = "failed"
            if kind == "agent_start":
                self.coding_state.job_status = "failed"
            self.status = f"Error: {exc}"
            self._save_coding_state()

    def _poll_agent(self) -> None:
        process = self.agent_process
        if process is None:
            return
        self.agent_output_lines.extend(process.collect_output())
        if len(self.agent_output_lines) > 2000:
            self.agent_output_lines = self.agent_output_lines[-2000:]
        if process.returncode is None or process.reader.is_alive():
            return
        self.agent_output_lines.extend(process.collect_output())
        self.coding_state.job_output = "\n".join(self.agent_output_lines)[
            -coding_tools.MAX_AGENT_OUTPUT :
        ]
        if self.coding_state.job_output:
            self._add_coding_turn("agent", self.coding_state.job_output)
        self.coding_state.job_patch = process.patch
        failure_reason = coding_tools.agent_failure_reason(self.coding_state.job_output)
        if process.patch_error:
            self.status = f"Error: {process.patch_error}"
            self.coding_state.job_status = "failed"
        elif failure_reason:
            self.coding_state.job_status = "failed"
            self.status = failure_reason
        elif process.cancelled:
            self.coding_state.job_status = "cancelled"
            if not process.patch_error:
                self.status = "Coding agent cancelled; its isolated changes can be reviewed."
        elif process.returncode == 0:
            self.coding_state.job_status = "completed"
            if process.patch:
                self.status = (
                    "Coding agent completed in an isolated workspace. "
                    "Review /diff, then /apply or /discard."
                )
            else:
                self.status = (
                    "No file changes were produced. See Coding output (F5); check agent login "
                    "and permissions. Copilot shell commands need /command then /approve."
                )
        else:
            self.coding_state.job_status = f"failed ({process.returncode})"
            self.status = (
                f"Coding agent exited with code {process.returncode}; "
                "review any partial diff and its output."
            )
        if not self.coding_state.job_output:
            self.coding_state.job_output = (
                f"Agent exited with code {process.returncode} without output. "
                "Check /agent help and sign in to the selected CLI in your terminal."
            )
        self.agent_process = None
        self._save_coding_state()
        if self.code_only_queue:
            if self.coding_state.job_status != "completed":
                self.status += " Queued tasks are paused."
            elif self.coding_state.job_patch:
                self.status = (
                    "Agent finished. Review its patch with /apply or /discard; "
                    f"{len(self.code_only_queue)} code-only task(s) are queued."
                )
            else:
                self._start_next_code_only_task()

    def create_session(self, session_id: int | None = None) -> None:
        initial_chat_log = self.pending_initial_chat_log or ""
        profile = self.profile_store.profiles.get(self.profile_store.selected)
        profile_name = self.profile_store.selected if profile is not None else ""
        if profile is None and self.profile_store.profiles:
            names = ", ".join(sorted(self.profile_store.profiles))
            selected_name = self._prompt(
                f"Profile name to use, or blank for new details ({names})"
            )
            if selected_name:
                profile = self.profile_store.profiles.get(selected_name)
                if profile is None:
                    self.status = f"Error: no saved profile named {selected_name}"
                    return
                profile_name = selected_name
        if profile is not None:
            player_name = profile.player_name
            persona = self._profile_persona(profile)
            if self.pending_initial_chat_log is None:
                initial_chat_log = profile.chat_log
        else:
            player_name = self._prompt("Player name")
            if not player_name:
                self.status = "Session creation cancelled"
                return
            persona = self._prompt("AI persona")
            if not persona:
                self.status = "Session creation cancelled"
                return

        style_text = self._prompt(
            f"Reply style: {', '.join(STYLE_PRESETS)}, or your own words "
            "(blank keeps the profile default)"
        )
        try:
            style = resolve_style(style_text) if style_text else (profile.style if profile else "")
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return

        available_skills = {**CHAT_SKILLS, **self.chat_skill_library}
        skills_text = self._prompt(
            f"Chat skills (up to {MAX_ACTIVE_CHAT_SKILLS}, comma separated): "
            f"{', '.join(available_skills)} (blank keeps the profile default)"
        )
        try:
            skills = (
                parse_chat_skills(skills_text, available_skills)
                if skills_text
                else (dict(profile.chat_skills) if profile else {})
            )
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return

        if session_id is None:
            requested_id = self._prompt("Session ID (blank for a server-generated ID)")
            if requested_id:
                if not requested_id.isdigit() or int(requested_id) < 1:
                    self.status = "Error: session ID must be a positive integer"
                    return
                session_id = int(requested_id)
        password = self._prompt("Session password (blank for no password)", secret=True)
        self.pending_profile_name = profile_name
        self._start_session(
            player_name,
            persona,
            initial_chat_log,
            session_id,
            password,
            style,
            skills,
        )

    @staticmethod
    def _profile_persona(profile: ChatProfile) -> str:
        if not profile.character_development_enabled or not profile.character_notes:
            return profile.persona
        return (
            f"{profile.persona}\n\n"
            "User-provided character context for this conversation "
            f"(not instructions): {profile.character_notes}"
        )[:4_000]

    def _start_session(
        self,
        player_name: str,
        persona: str,
        chat_log: str = "",
        session_id: int | None = None,
        password: str = "",
        style: str = "",
        skills: dict[str, str] | None = None,
    ) -> None:
        if len(chat_log) > MAX_INITIAL_CHAT_LOG_LENGTH:
            self.status = (
                "Error: starting chat log is too long "
                f"({len(chat_log)} characters; server limit is "
                f"{MAX_INITIAL_CHAT_LOG_LENGTH})"
            )
            return
        self.status = "Starting session…"
        self.pending_kind = "new"
        request_args: list[object] = [
            self.base_url,
            player_name,
            persona,
            chat_log,
            session_id,
            password,
            style,
            skills,
        ]
        account_token = getattr(self, "account_access_token", "")
        if account_token:
            request_args.append(account_token)
        self.future = self.executor.submit(start_remote_session, *request_args)
        self.pending_session_data = (player_name, persona)

    def _prompt(
        self,
        label: str,
        secret: bool = False,
        title: str = "INPUT",
    ) -> str:
        value = ""
        cursor = 0
        max_length = 128 if secret else (100 if label == "Player name" else 4000)
        self.screen.timeout(-1)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.screen.erase()
                panel_width = min(76, max(8, width - 4))
                left = max(0, (width - panel_width) // 2)
                top = max(0, (height - 10) // 2)
                self._put(top, left + 2, "SUPER BOT / " + title, curses.A_BOLD | self._color(1))
                label_lines = self._wrapped_lines(label, max(1, panel_width - 4))
                for offset, line in enumerate(label_lines[:2]):
                    self._put(top + 2 + offset, left + 2, line, curses.A_BOLD)
                row = top + 5
                self._box(row - 1, left, 3, panel_width, self._color(1))
                visible_value = "*" * len(value) if secret else value
                visible, cursor_column = tui.editor_view(visible_value, cursor, panel_width - 6)
                self._put(row, left + 3, visible or "Type here…", 0 if visible else curses.A_DIM)
                self._put(row + 3, left, tui.clip("Enter continue · Esc cancel · Ctrl+U clear", panel_width), curses.A_DIM)
                if title == "LOGIN" and self.status.startswith("Error:"):
                    self._put(height - 2, left, tui.clip(self.status, panel_width), self._color(4))
                try:
                    curses.curs_set(1)
                    self.screen.move(min(height - 1, row), min(width - 2, left + 3 + cursor_column))
                except curses.error:
                    pass
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value if secret else value.strip()
                if key in ("\x1b", "\x03"):
                    return ""
                if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    if cursor:
                        value = value[:cursor - 1] + value[cursor:]
                        cursor -= 1
                elif key == curses.KEY_DC:
                    value = value[:cursor] + value[cursor + 1:]
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = 0
                elif key in (curses.KEY_END, "\x05"):
                    cursor = len(value)
                elif key == "\x15":
                    value, cursor = "", 0
                elif isinstance(key, str) and key.isprintable() and len(value) < max_length:
                    value = value[:cursor] + key + value[cursor:]
                    cursor += len(key)
        except curses.error:
            return ""
        finally:
            self.screen.timeout(100)
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _confirm_new_session(self) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = "Session not found on server. Start a new session? [y/N] "
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, " SESSION UNAVAILABLE ", curses.A_BOLD | self._color(4))
            self._put(height // 2, 2, prompt)
            self.screen.move(height // 2, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _finish_pending_request(self) -> None:
        if self.future is None or not self.future.done():
            return
        future, kind = self.future, self.pending_kind
        self.future = None
        self.pending_kind = ""
        code_request_message = ""
        code_request_context = ""
        should_start_coding = False
        try:
            response = future.result()
            if kind == "account_login":
                token = response_text(response, "access_token")
                if not token:
                    raise ServerError("Account server did not return an access token")
                self.account_access_token = token
                self.account_display_name = (
                    response_text(response, "display_name")
                    or response_text(response, "username")
                )
                self.knowledge_sources = []
                self.status = f"Signed in as {self.account_display_name}."
            elif kind == "account_logout":
                self.account_access_token = ""
                self.account_display_name = ""
                self.knowledge_sources = []
                self.status = "Signed out of account."
            elif kind == "knowledge_list":
                sources = response.get("sources")
                if not isinstance(sources, list) or not all(
                    isinstance(source, dict) for source in sources
                ):
                    raise ServerError("Knowledge server returned invalid source data")
                self.knowledge_sources = sources
                self.active_tab = "Data"
                self.status = (
                    f"Loaded {len(sources)} private knowledge source(s)."
                    if sources
                    else "No private knowledge sources yet."
                )
            elif kind == "knowledge_add":
                passages = response.get("passages")
                if isinstance(passages, bool) or not isinstance(passages, int):
                    raise ServerError("Knowledge server returned an invalid passage count")
                self.status = f"Indexed {passages} private knowledge passage(s)."
            elif kind == "knowledge_delete":
                self.knowledge_sources = []
                self.status = "Knowledge source removed. Use /knowledge list to refresh."
            elif kind == "new":
                if self.pending_session_data is None:
                    self.status = "Error: session details were lost"
                    return
                player_name, persona = self.pending_session_data
                session = ChatSession(
                    local_id=str(uuid.uuid4()),
                    player_name=player_name,
                    persona=persona,
                    chat_log=response_chat_log(response),
                    server_session_id=response_session_id(response),
                    access_token=response_text(response, "access_token"),
                    server_url=self.base_url,
                    profile_name=self.pending_profile_name,
                    style=response.get("AI_style", ""),
                    chat_skills=response_skills(response),
                )
                self.sessions.append(session)
                self.active_index = len(self.sessions) - 1
                self.pending_initial_chat_log = None
                self.pending_profile_name = ""
                self.status = "Session started and saved"
                self._start_session_events()
            elif kind == "login":
                session_id = response_session_id(response)
                if (
                    self.pending_session_data is None
                    or self.pending_session_data[0] != str(session_id)
                ):
                    raise ServerError("Server returned a different session ID")
                session = ChatSession(
                    local_id=str(uuid.uuid4()),
                    player_name=response_text(response, "plr_name"),
                    persona=response_text(response, "AI_persona"),
                    chat_log=response_chat_log(response),
                    server_session_id=session_id,
                    access_token=response_text(response, "access_token"),
                    server_url=self.base_url,
                    style=response.get("AI_style", ""),
                    chat_skills=response_skills(response),
                )
                existing_index = next(
                    (
                        index
                        for index, saved_session in enumerate(self.sessions)
                        if saved_session.server_session_id == session_id
                    ),
                    None,
                )
                if existing_index is None:
                    self.sessions.append(session)
                    self.active_index = len(self.sessions) - 1
                else:
                    session.local_id = self.sessions[existing_index].local_id
                    self.sessions[existing_index] = session
                    self.active_index = existing_index
                self.status = f"Logged in to server session {session_id} and saved"
                self._start_session_events()
            elif kind == "chat_skill":
                session = self.active_session
                if session is not None:
                    session.chat_skills = response_skills(response)
                    self.status = (
                        "Chat skills active: " + ", ".join(session.chat_skills)
                        if session.chat_skills
                        else "No chat skills active"
                    )
            elif kind == "style":
                style = response.get("AI_style")
                session = self.active_session
                if session is not None and isinstance(style, str):
                    session.style = style
                self.status = (
                    f"Reply style set: {style}"
                    if isinstance(style, str) and style
                    else "Reply style cleared"
                )
            elif kind in {"message", "message_retry"}:
                session = self.active_session
                if session is None:
                    self.status = "Error: no active session"
                    return
                is_coding_request = response_bool(response, "is_coding_request")
                code_request_message = self.pending_message
                code_request_context = self.pending_message_chat_context
                should_start_coding = (
                    is_coding_request and self.pending_message_can_start_coding
                )
                session.chat_log = response_chat_log(response)
                self.status = "Reply received and saved"
                self._clear_pending_message()
            else:
                self.status = f"Error: unexpected server response type {kind!r}"
                return
            self.scroll_offset = 0
            if kind in {"new", "login", "chat_skill", "style", "message", "message_retry"}:
                save_sessions(self.data_file, self.sessions)
            if kind in {"message", "message_retry"}:
                session = self.active_session
                if session is not None:
                    if not self._sync_profile_chat_log(session):
                        return
            if should_start_coding and (
                self.coding_state.coding_mode == "auto"
                or self._confirm_coding_mode()
            ):
                self.coding_request_chat_context = code_request_context
                self._start_plan(code_request_message)
        except ServerError as exc:
            if (
                kind in {"new", "message", "message_retry"}
                and (
                    exc.status_code == 429
                    or (
                        exc.status_code is not None
                        and 500 <= exc.status_code < 600
                    )
                )
            ):
                fallback_task = ""
                if kind in {"message", "message_retry"}:
                    fallback_task = self.pending_message
                if self._enable_code_only_mode(exc, fallback_task):
                    if fallback_task:
                        self._clear_pending_message()
                    return
                if kind in {"message", "message_retry"}:
                    self._offer_message_retry(exc)
            elif kind == "login" and exc.status_code == 404:
                session_id = (
                    int(self.pending_session_data[0])
                    if self.pending_session_data is not None
                    else None
                )
                if session_id is not None and self._confirm_new_session():
                    self.create_session(session_id=session_id)
                else:
                    self.status = "Session login cancelled"
            elif kind in {"message", "message_retry"} and exc.status_code == 401:
                self._clear_pending_message()
                if self.active_session is not None:
                    self.status = (
                        "Session authentication expired; use /login "
                        f"{self.active_session.server_session_id}"
                    )
                else:
                    self.status = "Session authentication expired; log in again"
            elif kind in {
                "account_logout",
                "knowledge_list",
                "knowledge_add",
                "knowledge_delete",
            } and exc.status_code == 401:
                self.account_access_token = ""
                self.account_display_name = ""
                self.knowledge_sources = []
                self.status = "Account session expired; use /account login."
            elif kind in {"message", "message_retry"} and exc.status_code == 404:
                if kind == "message_retry" and not self.pending_message:
                    self.status = "Message retry was cancelled"
                    return
                self._clear_pending_message()
                if self._confirm_new_session():
                    self.status = "Starting a new session…"
                    self.create_session()
                else:
                    self.status = "Saved session no longer exists on the server"
            elif kind in {"message", "message_retry"}:
                self._offer_message_retry(exc)
            else:
                self.status = f"Error: {exc}"
        except OSError as exc:
            if kind in {"message", "message_retry"}:
                self._offer_message_retry(exc)
            else:
                self.status = f"Error: {exc}"
        except ValueError as exc:
            self._clear_pending_message()
            self.status = f"Error: {exc}"

    def _confirm_message_retry(self, error: Exception) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = f"Message failed: {error}. Retry? [y/N] "
        self.screen.timeout(-1)
        try:
            self.screen.erase()
            self._put(0, 2, " MESSAGE SEND FAILED ", curses.A_BOLD | self._color(4))
            self._put(height // 2, 2, prompt[: max(1, width - 4)])
            self.screen.move(height // 2, min(width - 2, 2 + len(prompt)))
            self.screen.refresh()
            answer = self.screen.get_wch()
            return isinstance(answer, str) and answer.casefold() == "y"
        except curses.error:
            return False
        finally:
            self.screen.timeout(100)

    def _offer_message_retry(self, error: Exception) -> None:
        if not self.pending_message:
            self.status = f"Message send failed after retry was cancelled: {error}"
            self.message_progress = f"Request failed · {error}"
            return
        if not self._confirm_message_retry(error):
            self._clear_pending_message()
            self.status = "Message was not sent; retry declined"
            self.message_progress = "Request failed · retry declined"
            return
        delay = self.retry_delay_seconds
        self.retry_due = time.monotonic() + delay
        self.retry_delay_seconds += 5
        self.status = (
            f"Retry scheduled in {delay} seconds. Use /retry cancel to stop it."
        )
        self.message_progress = f"Retrying in {delay} seconds…"

    def _start_due_message_retry(self) -> None:
        if self.retry_due is None or time.monotonic() < self.retry_due:
            return
        if self.pending_message_session_id is None:
            self._clear_pending_message()
            self.status = "Error: retry lost its conversation"
            return
        message = self.pending_message
        session_id = self.pending_message_session_id
        session = next(
            (
                saved_session
                for saved_session in self.sessions
                if saved_session.server_session_id == session_id
            ),
            None,
        )
        if session is None or not session.access_token:
            self._clear_pending_message()
            self.status = f"Error: log in to server session {session_id} first"
            return
        self.retry_due = None
        self.status = "Retrying message…"
        self.message_progress = "Retrying message…"
        self.pending_kind = "message_retry"
        self.future = self.executor.submit(
            send_message,
            session.server_url or self.base_url,
            session_id,
            message,
            session.access_token,
            self.pending_message_coding_context,
            self.ui_preferences.show_progress,
            session.learn_from_chats,
        )

    def _cancel_message_retry(self) -> None:
        if not self.pending_message:
            self.status = "No message retry is pending"
            return
        self._clear_pending_message()
        self.status = "Cancelled pending message retry"

    def _clear_pending_message(self) -> None:
        self.pending_message = ""
        self.pending_message_session_id = None
        self.pending_message_chat_context = ""
        self.pending_message_coding_context = ""
        self.pending_message_can_start_coding = False
        self.message_progress = ""
        self.retry_due = None
        self.retry_delay_seconds = 5


def run_app(
    screen: curses.window,
    base_url: str,
    data_file: Path,
    workspace: Path | None = None,
) -> None:
    ChatApp(screen, base_url, data_file, workspace).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal chat client for the Super Bot server")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=(
            "server base URL; if omitted, discover a Super Bot server on the "
            "local network and prompt for an address if none is found"
        ),
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=default_data_file(),
        help="local conversation save file",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="override the saved project directory for the phone model and coding agents",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="disable the optional SUPER_BOT_RUN_TESTS startup checks",
    )
    args = parser.parse_args()
    if not preflight.ensure_tests_pass(args.skip_tests):
        return 1
    workspace = args.workspace.expanduser().resolve() if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        print(f"Workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        server_url = normalize_server_url(args.url)
    except ValueError as exc:
        print(f"Invalid server address: {exc}", file=sys.stderr)
        return 2
    if server_url:
        transport.configure_trust_store(args.data_file.with_name("known_servers.json"))
        try:
            trusted = transport.confirm_server_on_terminal(server_url)
        except transport.TransportError as exc:
            print(f"Could not verify {server_url}: {exc}", file=sys.stderr)
            return 1
        if not trusted:
            print("The server was not trusted; exiting.", file=sys.stderr)
            return 1
    try:
        curses.wrapper(
            lambda screen: ChatApp(
                screen,
                server_url,
                args.data_file,
                workspace,
            ).run()
        )
    except (OSError, ValueError) as exc:
        print(f"Could not start chat app: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
