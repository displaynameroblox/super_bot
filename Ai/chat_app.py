import argparse
import curses
from datetime import datetime
import json
import os
import re
import shlex
import socket
import sys
import subprocess
import textwrap
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
import uuid
from queue import Empty, Queue
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from . import coding_tools
except ImportError:
    import coding_tools


DEFAULT_URL = os.getenv("CHAT_SERVER_URL", "")
REQUEST_TIMEOUT = 500 #too much??
MAX_INITIAL_CHAT_LOG_LENGTH = 10_000
DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"SUPER_BOT_DISCOVER_V1"
DISCOVERY_RESPONSE_PREFIX = b"SUPER_BOT_SERVER_V1:"
SPEAKER_LINE = re.compile(
    r"^(?:\[(?P<timestamp>[^\]]+)\]\s*)?"
    r"(?P<speaker>[^:\n]{1,100}):(?:\s?(?P<text>.*))?$"
)
ACTION_MARKUP = re.compile(r"(\*\*.*?\*\*|\*.*?\*)")
HELP_TEXT = """\
COMMANDS

On launch, Super Bot shows a login prompt before opening the chat. Enter a
session ID to join, or type NEW to create a session.

/new
Start a new conversation. Choose a saved profile or enter a player name and AI
persona, then optionally choose a session ID and password.

/login [SESSION_ID]
Join a server session using its ID and optional password. If it does not exist,
you can create it. Example: /login 23

/connect
Discover Super Bot servers on the local network, or enter a server address.
Discovery is local-network only; enter a reachable address for other networks.

/list
Show locally saved conversations. The number shown is used with /open.

/open
Open the current conversation, or start a new session if none have been created.

/open N
Switch to a locally saved conversation by its number from /list.
Example: /open 1

/profile list
List saved player/persona profiles.
/profile save NAME
Save the current conversation's player name, AI persona, and chat log.
/profile use NAME
Use a saved profile automatically when creating the next session.
/profile clear
Stop using the selected profile; /new will ask for details again.
/profile delete NAME
Delete a saved profile.

/log load session N
Use the chat log from a locally saved session as the starting history for /new.
/log load profile NAME
Use a profile's saved chat log as the starting history for /new.
Quote profile names containing spaces, for example: /log load profile "Friendly cat".
/log clear
Clear the staged starting chat log.

/retry cancel
Cancel a message retry that is waiting for its backoff delay.
After a send failure, the app asks before retrying; confirmed retries wait
5 seconds, then 10, then 15, increasing by 5 seconds each time.

/help
Show this command guide.

/quit
Close the app.

CODING MODE
/code REQUEST
Explicitly ask Gemini to prepare a coding brief from your request and coding-mode history.
/refine FEEDBACK
Discuss feedback with Gemini and update the brief. Use /plan to review it.
/run
Send the reviewed brief and selected skills to your configured coding CLI.
/coding-mode brief|confirm|auto
Choose how Gemini routes coding requests. brief prepares a brief only; confirm
asks before starting the agent; auto starts the agent after preparing the brief.
Auto mode still leaves patch review and application to you.
Use /coding-mode without an argument to show the current setting.
/status
Show the current plan and coding-agent status.
/diff
Review the coding agent's isolated patch, or current workspace diff.
/diff workspace
Show the existing working-tree diff instead of the agent patch.
/apply
Apply the reviewed agent patch to the workspace after a safe conflict check.
/discard
Discard the agent patch without changing the workspace.
/cancel
Stop the active coding agent.
/suggest
Ask Gemini to suggest a bounded coding task for this workspace.

/workspace [PATH]
Choose the project root Gemini and coding agents should use. Without PATH,
enter it when prompted. The selected folder is saved for the next launch.

/agent list
List supported coding CLIs and show which are installed.
/agent use NAME
Select copilot, claude, or codex.
/agent configure
Set the selected CLI command template; include {prompt} where the brief goes.
/agent help
Show the selected CLI's native --help output.
/agent install NAME
Show the selected provider's official installation page. Installation is manual.

/skill list
List built-in, discovered, and user-created skills.
/skill use NAME
Add a skill to Gemini's coding-request detection and brief, and the agent prompt.
/skill create NAME INSTRUCTIONS
Create a user skill shared by Gemini and any selected coding agent.
/skill edit NAME INSTRUCTIONS
Edit one of your custom skills.
/skill delete NAME
Delete one of your custom skills.
/skill show NAME
Show the full instructions for a built-in, discovered, or custom skill.
/skill clear
Clear selected skills.
/view chat|coding|split
Choose chat-focused, coding-focused, or split display.
/command REQUEST
Ask Gemini to propose one command. Review it, then use /approve or /reject.
/approve
Approve and run the displayed command without a shell in this workspace.
/reject
Reject the pending command proposal.

CHAT AND NAVIGATION
Type a message and press Enter to send it to the selected conversation.
Gemini checks whether ordinary messages are coding requests; if so, it asks
before opening the coding workflow. Use /code to enter coding mode directly.
Use Up/Down or Page Up/Page Down to scroll through the chat.
Home scrolls to the beginning; End returns to the latest messages.

Conversations are saved locally after successful server responses. A session
must still exist in the server's memory to send messages or log in to it.
Coding agents run in the workspace and can change files. Review /diff; there are
no automatic commits. Commands proposed by Gemini require explicit approval.
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


@dataclass
class ChatProfile:
    player_name: str
    persona: str
    chat_log: str = ""


@dataclass
class ProfileStore:
    profiles: dict[str, ChatProfile]
    selected: str = ""


def default_data_file() -> Path:
    if sys.platform == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "super_bot"
    elif os.name == "nt":
        data_dir = Path(os.getenv("APPDATA", Path.home())) / "super_bot"
    else:
        data_dir = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share")) / "super_bot"
    return data_dir / "chat_sessions.json"


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
            profiles[name] = ChatProfile(
                player_name=profile["player_name"],
                persona=profile["persona"],
                chat_log=profile.get("chat_log", ""),
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


def request_json(
    base_url: str,
    endpoint: str,
    payload: dict[str, object] | None = None,
    access_token: str = "",
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{endpoint}",
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except (UnicodeError, json.JSONDecodeError, AttributeError):
            detail = str(exc)
        raise ServerError(
            f"Server returned HTTP {exc.code}: {detail}",
            status_code=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ServerError(f"Could not reach the server: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ServerError(f"Server returned invalid JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ServerError("Server returned an unexpected response")
    return result


def start_remote_session(
    base_url: str,
    player_name: str,
    persona: str,
    chat_log: str = "",
    session_id: int | None = None,
    password: str = "",
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
        },
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
) -> dict[str, Any]:
    return request_json(
        base_url,
        "/chat",
        {
            "ses_id": session_id,
            "msg": message,
            "coding_context": coding_context,
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
                    found.add(f"http://{address}:{port}")
    except OSError:
        return []
    return sorted(found)


def normalize_server_url(address: str) -> str:
    value = address.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
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
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        events.put(
            {
                "type": "error",
                "text": "Live updates require the websockets package; install Ai/requirements.txt",
            }
        )
        return

    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    websocket_url = urlunsplit(
        (scheme, parsed.netloc, f"/events/{session_id}", "", "")
    )
    error_reported = False
    while not stop_event.is_set():
        try:
            with connect(
                websocket_url,
                additional_headers={"Authorization": f"Bearer {access_token}"},
                open_timeout=5,
                close_timeout=1,
            ) as websocket:
                error_reported = False
                while not stop_event.is_set():
                    try:
                        payload = websocket.recv(timeout=1)
                    except TimeoutError:
                        continue
                    if isinstance(payload, str):
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict) and all(
                            isinstance(key, str) and isinstance(value, str)
                            for key, value in event.items()
                        ):
                            events.put(event)
        except (OSError, TimeoutError, WebSocketException) as exc:
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
        match = SPEAKER_LINE.match(line)
        if match:
            save_message()
            speaker = match.group("speaker").strip()
            timestamp = match.group("timestamp")
            message_lines = [match.group("text") or ""]
        else:
            message_lines.append(line)
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
        self.sessions = load_sessions(data_file)
        self.active_index = len(self.sessions) - 1
        self.input_text = ""
        self.status = "Ready"
        self.scroll_offset = 0
        self.coding_scroll_offset = 0
        self.help_visible = False
        self.view_mode = "split"
        self.future: Future[dict[str, Any]] | None = None
        self.pending_kind = ""
        self.pending_session_data: tuple[str, str] | None = None
        self.pending_initial_chat_log: str | None = None
        self.pending_message = ""
        self.pending_message_session_id: int | None = None
        self.pending_message_chat_context = ""
        self.pending_message_coding_context = ""
        self.pending_message_can_start_coding = False
        self.retry_delay_seconds = 5
        self.retry_due: float | None = None
        self.coding_data_path = data_file.with_name("coding_state.json")
        self.profiles_path = data_file.with_name("chat_profiles.json")
        self.profile_store = load_profiles(self.profiles_path)
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
            self.status = "A saved Gemini command awaits your explicit /approve or /reject."

    def _connect_to_server(self) -> bool:
        try:
            servers = discover_servers()
        except ValueError as exc:
            self.status = f"Error: {exc}"
            return False
        if len(servers) == 1:
            self.base_url = servers[0]
            self.status = f"Discovered Super Bot server at {self.base_url}"
            return True

        if servers:
            choices = "; ".join(
                f"{index + 1}: {url}" for index, url in enumerate(servers)
            )
            answer = self._prompt(
                f"Choose server number ({choices}) or enter address",
                title="LOGIN",
            )
            if answer.isdigit() and 1 <= int(answer) <= len(servers):
                self.base_url = servers[int(answer) - 1]
                self.status = f"Connected to {self.base_url}"
                return True
            address = answer
        else:
            address = self._prompt(
                "Server address or IP (for example 192.168.1.20:8000)",
                title="LOGIN",
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
        self.status = f"Using server {self.base_url}"
        return True

    def _start_session_events(self) -> None:
        self._stop_session_events()
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

    def _poll_session_events(self) -> None:
        while True:
            try:
                event = self.session_events.get_nowait()
            except Empty:
                return
            event_type = event.get("type", "")
            if event_type == "message":
                session = self.active_session
                chat_log = event.get("chat_log")
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
                        self.status = (
                            f"{event.get('player_name', 'A client')} sent a message"
                        )
            elif event_type == "joined":
                self.status = f"{event.get('player_name', 'A client')} joined this session"
            elif event_type == "error":
                self.status = f"Live update connection interrupted: {event.get('text', '')}"

    def _startup_login(self) -> bool:
        while not self.closed:
            session_text = self._prompt(
                "Session ID to log in (or type NEW to create a session)",
                title="LOGIN",
            )
            if not session_text:
                self.status = "Login cancelled"
                return False
            if session_text.casefold() == "new":
                self.create_session()
                self._wait_for_startup_request()
                return self.active_session is not None
            if not session_text.isdigit() or int(session_text) < 1:
                self.status = "Error: session ID must be a positive integer, or type NEW"
                continue

            session_id = int(session_text)
            password = self._prompt("Session password (leave blank if none)", secret=True, title="LOGIN")
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

    def _setup_screen(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)

    @property
    def active_session(self) -> ChatSession | None:
        if 0 <= self.active_index < len(self.sessions):
            return self.sessions[self.active_index]
        return None

    def run(self) -> None:
        try:
            if not self.base_url and not self._connect_to_server():
                return
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
            self._stop_session_events()
            if self.agent_process is not None:
                self.agent_process.cancel()
                self.coding_state.job_status = "interrupted"
                self._save_coding_state()
            self.executor.shutdown(wait=False, cancel_futures=True)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 10 or width < 50:
            self._put(0, 0, "Resize the terminal to at least 50 columns by 10 rows.")
            self.screen.refresh()
            return

        self._put(0, 2, " SUPER BOT  /  CHAT ", curses.A_BOLD | self._color(1))
        session = self.active_session
        if session is None:
            subtitle = "No session selected"
        else:
            subtitle = (
                f"{session.player_name}  ·  session {session.server_session_id}  "
                f"·  {self.active_index + 1}/{len(self.sessions)}"
            )
        if self.help_visible:
            subtitle = "Command guide  ·  ↑/↓ scroll  ·  Esc close"
        elif self.agent_process is not None:
            subtitle += f"  ·  {self.coding_state.provider} agent running"
        self._put(1, 2, subtitle, self._color(1))
        self._put(2, 1, "─" * (width - 2), curses.A_DIM)

        body_top = 3
        body_height = height - 7
        if self.help_visible:
            lines = [[(line, "normal")] for line in self._wrapped_lines(HELP_TEXT, width - 6)]
            self.scroll_offset = min(self.scroll_offset, max(0, len(lines) - body_height))
            end = max(0, len(lines) - self.scroll_offset)
            start = max(0, end - body_height)
            for row, line in enumerate(lines[start:end], start=body_top):
                self._draw_rich_line(row, 3, line, width - 6)
        elif self.view_mode == "split" and width >= 90:
            column_width = (width - 5) // 2
            self._put(body_top, 2, " CHAT ", curses.A_BOLD | self._color(1))
            self._put(
                body_top,
                column_width + 3,
                " CODING / AGENT ",
                curses.A_BOLD | self._color(2),
            )
            separator_x = column_width + 1
            for row in range(body_top + 1, height - 4):
                self._put(row, separator_x, "│", curses.A_DIM)
            content_top = body_top + 1
            content_height = height - content_top - 4
            chat_lines = self._chat_panel_lines(column_width - 3)
            coding_lines = self._coding_panel_lines(column_width - 3)
            self.scroll_offset = min(
                self.scroll_offset, max(0, len(chat_lines) - content_height)
            )
            self.coding_scroll_offset = min(
                self.coding_scroll_offset, max(0, len(coding_lines) - content_height)
            )
            chat_end = max(0, len(chat_lines) - self.scroll_offset)
            coding_end = max(0, len(coding_lines) - self.coding_scroll_offset)
            for row, line in enumerate(
                chat_lines[max(0, chat_end - content_height) : chat_end],
                content_top,
            ):
                self._draw_rich_line(row, 2, line, column_width - 3)
            for row, line in enumerate(
                coding_lines[max(0, coding_end - content_height) : coding_end],
                content_top,
            ):
                self._draw_rich_line(
                    row,
                    column_width + 3,
                    line,
                    width - column_width - 5,
                )
        elif self.view_mode == "coding":
            lines = self._coding_panel_lines(width - 6)
            self.coding_scroll_offset = min(
                self.coding_scroll_offset, max(0, len(lines) - body_height)
            )
            end = max(0, len(lines) - self.coding_scroll_offset)
            start = max(0, end - body_height)
            for row, line in enumerate(lines[start:end], start=body_top):
                self._draw_rich_line(row, 3, line, width - 6)
        else:
            lines = self._chat_panel_lines(width - 6)
            self.scroll_offset = min(self.scroll_offset, max(0, len(lines) - body_height))
            end = max(0, len(lines) - self.scroll_offset)
            start = max(0, end - body_height)
            for row, line in enumerate(lines[start:end], start=body_top):
                self._draw_rich_line(row, 3, line, width - 6)

        self._put(height - 4, 1, "─" * (width - 2), curses.A_DIM)
        status_attr = self._color(4) if self.status.startswith("Error:") else self._color(3)
        status = "Help is open; press Esc to return to chat" if self.help_visible else self.status
        self._put(height - 3, 2, status, status_attr)
        input_label = " MESSAGE › "
        self._put(height - 2, 2, input_label, curses.A_BOLD | self._color(2))
        self._put(height - 2, 2 + len(input_label), self.input_text, curses.A_BOLD)
        self._put(
            height - 1,
            1,
            "Enter send · /code · /run · /status · /view · /help · /quit · ↑/↓",
            curses.A_DIM,
        )
        cursor_x = min(width - 2, 2 + len(input_label) + len(self.input_text))
        if not self.future:
            try:
                curses.curs_set(1)
                self.screen.move(height - 2, cursor_x)
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        self.screen.refresh()

    def _chat_panel_lines(self, width: int) -> list[list[tuple[str, str]]]:
        session = self.active_session
        if session is None or not session.chat_log:
            return [[("Start a conversation by typing below.", "dim")]]
        return self._transcript_lines(session, width - 2)

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
            lines.append([("No coding brief. Use /code <request>.", "dim")])
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
                [(line, "action" if line.startswith(("+", "-")) else "dim")]
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
        session: ChatSession,
        width: int,
    ) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []
        for message in parse_chat_log(session.chat_log):
            is_ai = message.speaker.casefold() in {"ai", "assistant"}
            name = "AI" if is_ai else session.player_name
            header_style = "ai" if is_ai else "player"
            timestamp = format_timestamp(message.timestamp)
            lines.append([(name, header_style), ("  ·  ", "dim"), (timestamp, "dim")])
            for source_line in message.text.splitlines() or [""]:
                lines.extend(
                    self._wrap_rich_segments(
                        [("  ", "dim"), *style_message_text(source_line)],
                        width,
                    )
                )
            lines.append([("", "normal")])
        return lines

    def _wrap_rich_segments(
        self,
        segments: list[tuple[str, bool | str]],
        width: int,
    ) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = [[]]
        line_length = 0
        for text, style in segments:
            style_name = (
                "action" if style is True else "normal" if style is False else style
            )
            for token in re.findall(r"\n|[^\S\n]+|[^\s]+", text):
                if token == "\n":
                    lines.append([])
                    line_length = 0
                    continue
                if token.isspace():
                    if line_length + len(token) > width:
                        lines.append([])
                        line_length = 0
                        continue
                elif line_length and line_length + len(token) > width:
                    lines.append([])
                    line_length = 0

                while len(token) > width:
                    chunk, token = token[:width], token[width:]
                    lines[-1].append((chunk, style_name))
                    lines.append([])
                    line_length = 0
                if token:
                    lines[-1].append((token, style_name))
                    line_length += len(token)
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
        }
        column = x
        for text, style in segments:
            if column >= x + width:
                break
            visible_text = text[: x + width - column]
            self._put(y, column, visible_text, attributes[style])
            column += len(visible_text)

    def _put(self, y: int, x: int, text: str, attributes: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width:
            try:
                self.screen.addnstr(y, x, text, width - x - 1, attributes)
            except curses.error:
                pass

    @staticmethod
    def _color(pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    def handle_key(self, key: int | str) -> None:
        if self.help_visible:
            if key == "\x1b":
                self.help_visible = False
                self.scroll_offset = 0
            elif key == curses.KEY_UP:
                self.scroll_offset += 1
            elif key == curses.KEY_DOWN:
                self.scroll_offset = max(0, self.scroll_offset - 1)
            elif key == curses.KEY_PPAGE:
                self.scroll_offset += max(1, self.screen.getmaxyx()[0] - 8)
            elif key == curses.KEY_NPAGE:
                self.scroll_offset = max(
                    0, self.scroll_offset - max(1, self.screen.getmaxyx()[0] - 8)
                )
            elif key in (curses.KEY_HOME,):
                self.scroll_offset = 10**9
            elif key == curses.KEY_END:
                self.scroll_offset = 0
            return
        if self.future and self.pending_kind != "message_retry":
            return
        if key in ("\n", "\r", curses.KEY_ENTER):
            self.submit_input()
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.input_text = self.input_text[:-1]
        elif key == curses.KEY_UP:
            if self.view_mode == "coding":
                self.coding_scroll_offset += 1
            else:
                self.scroll_offset += 1
        elif key == curses.KEY_DOWN:
            if self.view_mode == "coding":
                self.coding_scroll_offset = max(0, self.coding_scroll_offset - 1)
            else:
                self.scroll_offset = max(0, self.scroll_offset - 1)
        elif key == curses.KEY_PPAGE:
            amount = max(1, self.screen.getmaxyx()[0] - 8)
            if self.view_mode == "coding":
                self.coding_scroll_offset += amount
            else:
                self.scroll_offset += amount
        elif key == curses.KEY_NPAGE:
            amount = max(1, self.screen.getmaxyx()[0] - 8)
            if self.view_mode == "coding":
                self.coding_scroll_offset = max(0, self.coding_scroll_offset - amount)
            else:
                self.scroll_offset = max(0, self.scroll_offset - amount)
        elif key == curses.KEY_HOME:
            if self.view_mode == "coding":
                self.coding_scroll_offset = 10**9
            else:
                self.scroll_offset = 10**9
        elif key == curses.KEY_END:
            if self.view_mode == "coding":
                self.coding_scroll_offset = 0
            else:
                self.scroll_offset = 0
        elif key == "\x03":
            self.closed = True
        elif isinstance(key, str) and key.isprintable():
            self.input_text += key
            self.scroll_offset = 0

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
        session = self.active_session
        if session is None:
            self.status = "Error: create a session with /new first"
            return
        if len(value) > 1000:
            self.status = "Error: messages must be at most 1000 characters"
            return
        self.input_text = ""
        self._send_chat_message(value, session.server_session_id)

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
        self.retry_delay_seconds = 5
        self.status = "Waiting for Gemini/server reply…"
        self.pending_kind = "message"
        self.future = self.executor.submit(
            send_message,
            session.server_url or self.base_url,
            session_id,
            message,
            session.access_token,
            coding_context,
        )

    def _confirm_coding_mode(self) -> bool:
        height, width = self.screen.getmaxyx()
        prompt = "Gemini thinks this is a coding request. Start coding mode? [y/N] "
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
            if self._connect_to_server():
                self.status = f"Connected to {self.base_url}"
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
        elif action == "/profile":
            self._handle_profile_command(command.partition(" ")[2].strip())
        elif action == "/log":
            self._handle_log_command(command.partition(" ")[2].strip())
        elif action == "/retry" and len(parts) == 2 and parts[1].casefold() == "cancel":
            self._cancel_message_retry()
        elif action == "/code" and argument:
            self._start_plan(argument)
        elif action == "/refine" and argument:
            self._refine_plan(argument)
        elif action == "/plan":
            self.view_mode = "coding"
            self.coding_scroll_offset = 0
            self.status = (
                "Review the Gemini brief; use /refine <feedback> or /run."
                if self.coding_state.plan
                else "No plan yet. Use /code <request>."
            )
        elif action == "/run":
            self._run_agent()
        elif action == "/coding-mode":
            self._set_coding_mode(argument)
        elif action == "/status":
            self.view_mode = "coding"
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
                self.view_mode = "coding"
                self.coding_scroll_offset = 0
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
        elif action == "/view" and len(parts) == 2 and parts[1] in {
            "chat",
            "coding",
            "split",
        }:
            self.view_mode = parts[1]
            self.status = f"Changed view to {self.view_mode}"
        elif action == "/agent":
            self._handle_agent_command(parts[1:])
        elif action == "/skill":
            self._handle_skill_command(command.partition(" ")[2].strip())
        elif action == "/command" and argument:
            self.coding_state.command_request = argument
            self.coding_state.proposed_command = ""
            self.coding_state.command_output = ""
            self.coding_state.command_cwd = str(self.workspace)
            if self._start_coding_request(
                "command", coding_tools.suggest_command, self.workspace, argument
            ):
                self.status = "Asking Gemini for a command proposal…"
        elif action == "/approve":
            self._approve_command()
        elif action == "/reject":
            self._reject_command()
        elif action == "/quit":
            self.closed = True
        elif action == "/help":
            self.help_visible = True
            self.scroll_offset = 0
        else:
            self.status = "Error: unknown command; try /help"

    def _start_coding_request(self, kind: str, function: Any, *args: Any) -> bool:
        if self.coding_future is not None:
            self.status = "Error: another Gemini coding request is still running"
            return False
        self.coding_future_kind = kind
        self.coding_future = self.executor.submit(function, *args)
        return True

    def _start_plan(self, request: str) -> None:
        if self.coding_future is not None:
            self.status = "Error: another Gemini coding request is still running"
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
        self.coding_state.plan = ""
        self.coding_state.plan_workspace = str(self.workspace)
        self.coding_state.plan_status = "planning"
        self._add_coding_turn("user", request)
        chat_context = getattr(self, "coding_request_chat_context", "")
        self.coding_request_chat_context = ""
        self.view_mode = "coding"
        self.coding_scroll_offset = 0
        self.coding_future_kind = "plan"
        self.coding_future = self.executor.submit(
            coding_tools.make_plan,
            self.workspace,
            request,
            self.coding_state.coding_history[:-1],
            skills,
            chat_context=chat_context,
        )
        self.status = "Gemini is preparing a coding brief…"
        self._save_coding_state()

    def _refine_plan(self, feedback: str) -> None:
        if not self.coding_state.plan:
            self.status = "Error: create a plan first with /code <request>"
            return
        if self.coding_state.plan_workspace != str(self.workspace):
            self.status = "Error: this brief belongs to another workspace; create a new plan"
            return
        if self.coding_future is not None:
            self.status = "Error: another Gemini coding request is still running"
            return
        self._add_coding_turn("user", feedback)
        self.coding_state.plan_status = "refining"
        self.coding_future_kind = "refine"
        self.coding_future = self.executor.submit(
            coding_tools.refine_plan,
            self.workspace,
            self.coding_state.plan,
            feedback,
            coding_history=self.coding_state.coding_history[:-1],
        )
        self.status = "Gemini is updating the brief…"
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
            self.status = "Error: another Gemini coding request is still running"
            return
        if self.coding_state.job_patch:
            self.status = "Review the pending agent patch with /apply or /discard first"
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
            coding_tools.selected_skill_text(self.coding_state.skills, self.skills),
            workspace_context=f"Workspace file list:\n{file_list}",
        )
        self.view_mode = "coding"
        self.status = "Gemini is reviewing the workspace for suggestions…"
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
            self.view_mode = "coding"
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
            provider = coding_tools.PROVIDER_SPECS.get(args[1].lower())
            if provider is None or not provider["website"]:
                self.status = "Error: see /agent list or configure a custom CLI"
            else:
                self.coding_state.job_output = (
                    f"Install {provider['label']} manually from:\n"
                    f"{provider['website']}\n"
                    "The app never downloads or runs installers."
                )
                self.view_mode = "coding"
                self.status = f"Official installation page for {provider['label']} shown"
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
            self.profile_store.profiles[name] = ChatProfile(
                player_name=session.player_name,
                persona=session.persona,
                chat_log=session.chat_log,
            )
            self.profile_store.selected = name
            self.status = f"Saved and selected profile: {name}"
            self._save_profile_store()
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

    def _save_profile_store(self) -> None:
        try:
            save_profiles(self.profiles_path, self.profile_store)
        except OSError as exc:
            self.status = f"Error saving profiles: {exc}"

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
            self.view_mode = "coding"
            self.status = "Built-in and discovered skills shown in coding view"
        elif args[0] == "show" and len(args) == 2:
            if args[1] not in self.skills:
                self.status = f"Error: unknown skill '{args[1]}'; use /skill list"
                return
            self.coding_state.job_output = f"SKILL: {args[1]}\n\n{self.skills[args[1]]}"
            self.view_mode = "coding"
            self.status = f"Showing skill: {args[1]}"
        elif args[0] == "use" and len(args) == 2:
            if args[1] not in self.skills:
                self.status = f"Error: unknown skill '{args[1]}'; use /skill list"
                return
            if args[1] not in self.coding_state.skills:
                self.coding_state.skills.append(args[1])
            self.status = f"Selected skill for Gemini and agent: {args[1]}"
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
            self.status = "Error: review a Gemini brief before using /run"
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
        if not self.coding_state.command_template:
            self.status = "Configure an agent CLI first with /agent configure."
            return
        if self.coding_future is not None:
            self.status = "Error: wait for the active coding request to finish"
            return
        try:
            skills = coding_tools.selected_skill_text(
                self.coding_state.skills,
                self.skills,
            )
            prompt = (
                "Implement this user-approved task in the current workspace.\n\n"
                f"TASK BRIEF:\n{self.coding_state.plan}\n\n"
                f"SELECTED SKILLS:\n{skills}\n\n"
                "Apply these skills regardless of which coding CLI is selected. "
                "Inspect relevant files before editing. Preserve unrelated work "
                "and user changes. Do not read or reveal .env files, credentials, "
                "or secrets. Do not commit changes. Run focused validation and "
                "summarize changed files and test results."
            )
            self.coding_state.job_status = "preparing"
            self.coding_state.job_patch = ""
            self.coding_state.job_output = ""
            self.coding_state.job_command = self.coding_state.command_template
            self.coding_state.plan_status = "submitted"
            self.view_mode = "split"
            self.coding_future_kind = "agent_start"
            self.coding_future = self.executor.submit(
                coding_tools.AgentProcess,
                self.coding_state.command_template,
                prompt,
                self.workspace,
            )
        except (OSError, ValueError) as exc:
            self.status = (
                f"Error: could not start coding agent: {exc}. "
                "Check its installation and /agent configure."
            )
            return
        self.status = "Preparing isolated agent workspace…"
        self._save_coding_state()

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
            self.status = "Gemini request is already in progress and cannot be interrupted."
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
        self.status = "Rejected Gemini's command proposal"
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
                self.coding_state.plan = str(result)[: coding_tools.MAX_PLAN_LENGTH]
                self.coding_state.plan_status = "reviewing"
                if kind in {"plan", "refine"}:
                    self._add_coding_turn("assistant", self.coding_state.plan)
                self.view_mode = "coding"
                self.status = (
                    "Gemini brief ready. Discuss with /refine <feedback>; "
                    "use /run when you're happy with it."
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
                self.view_mode = "coding"
                self.status = (
                    "Gemini proposed a command. Review it in coding view, "
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
                self.view_mode = "coding"
                self.status = f"{self.coding_state.provider} help loaded"
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
                self.status = "Agent patch safely applied. Review your workspace with /diff."
            elif kind == "diff":
                self.coding_state.job_output = str(result)
                self.view_mode = "coding"
                self.coding_scroll_offset = 0
                self.status = "Workspace diff loaded"
            self.coding_scroll_offset = 0
            self._save_coding_state()
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
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
        if process.patch_error:
            self.status = process.patch_error
        if process.cancelled:
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
                self.status = "Coding agent completed without producing a diff."
        else:
            self.coding_state.job_status = f"failed ({process.returncode})"
            self.status = (
                f"Coding agent exited with code {process.returncode}; "
                "review any partial diff and its output."
            )
        self.agent_process = None
        self._save_coding_state()

    def create_session(self, session_id: int | None = None) -> None:
        initial_chat_log = self.pending_initial_chat_log or ""
        profile = self.profile_store.profiles.get(self.profile_store.selected)
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
        if profile is not None:
            player_name = profile.player_name
            persona = profile.persona
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

        if session_id is None:
            requested_id = self._prompt("Session ID (blank for a server-generated ID)")
            if requested_id:
                if not requested_id.isdigit() or int(requested_id) < 1:
                    self.status = "Error: session ID must be a positive integer"
                    return
                session_id = int(requested_id)
        password = self._prompt("Session password (blank for no password)", secret=True)
        self._start_session(
            player_name,
            persona,
            initial_chat_log,
            session_id,
            password,
        )

    def _start_session(
        self,
        player_name: str,
        persona: str,
        chat_log: str = "",
        session_id: int | None = None,
        password: str = "",
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
        self.future = self.executor.submit(
            start_remote_session,
            self.base_url,
            player_name,
            persona,
            chat_log,
            session_id,
            password,
        )
        self.pending_session_data = (player_name, persona)

    def _prompt(
        self,
        label: str,
        secret: bool = False,
        title: str = "INPUT",
    ) -> str:
        height, width = self.screen.getmaxyx()
        value = ""
        max_length = 128 if secret else (100 if label == "Player name" else 4000)
        self.screen.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                self.screen.erase()
                self._put(
                    0,
                    2,
                    f" SUPER BOT  /  {title} ",
                    curses.A_BOLD | self._color(1),
                )
                self._put(2, 2, f"Enter {label.lower()} (Esc to cancel)")
                if title == "LOGIN" and self.status.startswith("Error:"):
                    self._put(4, 2, self.status[: max(1, width - 4)], self._color(4))
                prompt = f"{label}: "
                available = max(1, width - len(prompt) - 4)
                visible_value = "*" * len(value) if secret else value
                visible = visible_value[-available:]
                self._put(height // 2, 2, prompt + visible, curses.A_BOLD)
                self.screen.move(height // 2, min(width - 2, 2 + len(prompt) + len(visible)))
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value if secret else value.strip()
                if key == "\x1b":
                    return ""
                if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                    value = value[:-1]
                elif isinstance(key, str) and key.isprintable() and len(value) < max_length:
                    value += key
        except curses.error:
            return ""
        finally:
            self.screen.timeout(100)
            curses.curs_set(0)

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
            if kind == "new":
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
                )
                self.sessions.append(session)
                self.active_index = len(self.sessions) - 1
                self.pending_initial_chat_log = None
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
            save_sessions(self.data_file, self.sessions)
            if should_start_coding and (
                self.coding_state.coding_mode == "auto"
                or self._confirm_coding_mode()
            ):
                self.coding_request_chat_context = code_request_context
                self._start_plan(code_request_message)
        except ServerError as exc:
            if kind == "login" and exc.status_code == 404:
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
            return
        if not self._confirm_message_retry(error):
            self._clear_pending_message()
            self.status = "Message was not sent; retry declined"
            return
        delay = self.retry_delay_seconds
        self.retry_due = time.monotonic() + delay
        self.retry_delay_seconds += 5
        self.status = (
            f"Retry scheduled in {delay} seconds. Use /retry cancel to stop it."
        )

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
        self.pending_kind = "message_retry"
        self.future = self.executor.submit(
            send_message,
            session.server_url or self.base_url,
            session_id,
            message,
            session.access_token,
            self.pending_message_coding_context,
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
        help="override the saved project directory for Gemini and coding agents",
    )
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve() if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        print(f"Workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        curses.wrapper(
            lambda screen: ChatApp(
                screen,
                args.url,
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
