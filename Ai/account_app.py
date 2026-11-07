"""Account-based terminal client for the Super Bot server.

A plain line-based Python app (no curses): sign in with a server account, then
start, resume, search and delete your chats. It talks to the server over the same
TLS + end-to-end sealed transport as ``chat_app`` and shares its server trust store,
so you verify the server's fingerprints once. Run with ``python -m Ai.account_app``.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from . import client_data, chat_app, preflight, tls, transport
except ImportError:
    from Ai import client_data
    import chat_app
    import preflight
    import tls
    import transport

REQUEST_TIMEOUT = 500
SPEAKER_LINE = re.compile(r"^(?:\[[^\]]+\]\s*)?(?P<speaker>[^:\n]{1,100}):(?:\s?(?P<text>.*))?$")
HELP = """\
Commands
  /new [persona]     start a new chat (optional persona text)
  /chats [search]    list your chats, optionally filtered by text
  /open ID           resume one of your chats
  /delete ID         delete one of your chats
  /profile NAME      change your display name
  /knowledge         list your indexed knowledge sources
  /knowledge add FILE [CATEGORY]
                    index a local text file in your private knowledge library
  /knowledge delete ID
                    remove one knowledge source
  /password          change your password
  /recovery          create a recovery code (shown once)
  /logout            sign out of this device
  /logout-all        sign out of every device
  /delete-account    permanently delete your account and chats
  /help              show this help
  /quit              leave
Anything else is sent as a message in the open chat."""


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


Requester = Callable[..., tuple[int, Any]]


class AccountClient:
    """Thin wrapper over the account API; ``requester`` is injectable for tests."""

    def __init__(self, base_url: str, requester: Requester | None = None) -> None:
        self.base_url = base_url
        self.requester = requester or client_data.request
        self.token = ""
        self.username = ""
        self.display_name = ""

    def call(self, method: str, path: str, body: Any = None, token: str | None = None) -> Any:
        try:
            status, result = self.requester(
                self.base_url, method, path, body,
                self.token if token is None else token,
                timeout=REQUEST_TIMEOUT,
            )
        except transport.TransportError as exc:
            raise ClientError(str(exc)) from exc
        if status >= 400:
            detail = result.get("detail") if isinstance(result, dict) else None
            if isinstance(detail, list):
                detail = "Please check the values you entered."
            raise ClientError(
                str(detail or http.client.responses.get(status, "Request failed")), status
            )
        if not isinstance(result, dict):
            raise ClientError("The server sent an unexpected response")
        return result

    def _adopt(self, grant: dict[str, Any]) -> None:
        self.token = grant["access_token"]
        self.username = grant["username"]
        self.display_name = grant.get("display_name") or grant["username"]

    def register(self, username: str, password: str, display_name: str = "") -> None:
        self._adopt(self.call("POST", "/accounts/register", {
            "username": username, "password": password, "display_name": display_name}, token=""))

    def login(self, username: str, password: str) -> None:
        self._adopt(self.call("POST", "/accounts/login", {
            "username": username, "password": password}, token=""))

    def recover(self, username: str, code: str, new_password: str) -> None:
        self.call("POST", "/accounts/recover", {
            "username": username, "recovery_code": code, "new_password": new_password}, token="")

    def logout(self, everywhere: bool = False) -> None:
        self.call("POST", "/accounts/logout_all" if everywhere else "/accounts/logout", {})
        self.token = ""

    def chats(self, query: str = "") -> list[dict[str, Any]]:
        return self.call("POST", "/accounts/chats", {"query": query})["chats"]

    def new_chat(self, persona: str, style: str = "") -> dict[str, Any]:
        return self.call("POST", "/start_ses", {
            "AI_persona": persona, "AI_style": style, "plr_name": self.display_name[:100]})

    def open_chat(self, session_id: int) -> dict[str, Any]:
        return self.call("POST", "/accounts/chats/open", {"session_id": session_id})

    def send(self, session_id: int, token: str, message: str) -> dict[str, Any]:
        return self.call("POST", "/chat", {
            "ses_id": session_id, "msg": message, "show_progress": False}, token=token)

    def delete_chat(self, session_id: int) -> None:
        self.call("POST", "/accounts/chats/delete", {"session_id": session_id})

    def knowledge_sources(self) -> list[dict[str, Any]]:
        return self.call("GET", "/accounts/knowledge")["sources"]

    def add_knowledge(
        self,
        *,
        source_url: str,
        title: str,
        content: str,
        category: str,
    ) -> int:
        response = self.call(
            "POST",
            "/accounts/knowledge/add",
            {
                "source_url": source_url,
                "title": title,
                "content": content,
                "category": category,
            },
        )
        return int(response["passages"])

    def delete_knowledge(self, source_id: int) -> None:
        self.call("POST", "/accounts/knowledge/delete", {"source_id": source_id})

    def set_display_name(self, name: str) -> None:
        profile = self.call("POST", "/accounts/profile", {"display_name": name})
        self.display_name = profile["display_name"] or self.username

    def change_password(self, current: str, new: str) -> None:
        self.call("POST", "/accounts/password", {"current_password": current, "new_password": new})

    def recovery_code(self, password: str) -> str:
        return self.call("POST", "/accounts/recovery_code", {"password": password})["recovery_code"]

    def delete_account(self, password: str) -> None:
        self.call("POST", "/accounts/delete", {"password": password})
        self.token = ""


def parse_log(log: str) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for line in log.splitlines():
        match = SPEAKER_LINE.match(line)
        if match:
            messages.append((match.group("speaker").strip(), match.group("text") or ""))
        elif messages:
            speaker, text = messages[-1]
            messages[-1] = (speaker, f"{text}\n{line}")
    return messages


def chat_title(chat: dict[str, Any]) -> str:
    last = parse_log(chat.get("preview") or "")
    text = last[-1][1].strip().replace("\n", " ") if last else ""
    return (text[:60] + "…") if len(text) > 60 else (text or "(empty)")


class AccountApp:
    def __init__(
        self,
        client: AccountClient,
        *,
        ask: Callable[[str], str] = input,
        ask_secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
        default_persona: str = "A friendly, helpful assistant.",
    ) -> None:
        self.client = client
        self.ask = ask
        self.ask_secret = ask_secret
        self.say = say
        self.default_persona = default_persona
        self.session_id: int | None = None
        self.session_token = ""

    def show_log(self, log: str, tail: int | None = None) -> None:
        messages = parse_log(log)
        for speaker, text in messages[-tail:] if tail else messages:
            label = "You" if speaker != "AI" else "Bot"
            self.say(f"{label}: {text}")

    def authenticate(self) -> bool:
        self.say("Super Bot — sign in with your account on this device.")
        while True:
            try:
                choice = self.ask("[L]ogin, [R]egister, re[C]over password, [Q]uit: ").strip().casefold()
            except EOFError:
                return False
            try:
                if choice in ("q", "quit"):
                    return False
                if choice in ("l", "login"):
                    self.client.login(self.ask("Username: ").strip(), self.ask_secret("Password: "))
                elif choice in ("r", "register"):
                    username = self.ask("Choose a username: ").strip()
                    display = self.ask("Display name (optional): ").strip()
                    password = self.ask_secret("Choose a password (8+ characters): ")
                    if self.ask_secret("Repeat password: ") != password:
                        self.say("Passwords did not match.")
                        continue
                    self.client.register(username, password, display)
                elif choice in ("c", "recover"):
                    username = self.ask("Username: ").strip()
                    code = self.ask("Recovery code: ").strip()
                    self.client.recover(username, code, self.ask_secret("New password: "))
                    self.say("Password reset. Sign in with your new password.")
                    continue
                else:
                    continue
            except ClientError as exc:
                self.say(f"Error: {exc}")
                continue
            self.say(f"Signed in as {self.client.display_name}.")
            return True

    def run(self) -> int:
        if not self.authenticate():
            return 0
        self.say("Type /help for commands. Use /new to start chatting.")
        while self.client.token:
            try:
                line = self.ask("> " if self.session_id is None else "you> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                return 0
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if self.command(line) is False:
                        return 0
                else:
                    self.message(line)
            except ClientError as exc:
                self.say(f"Error: {exc}")
                if exc.status == 401 and self.session_id is not None:
                    self.say("Reopening the chat to refresh its access…")
                    self.refresh_session()
        return 0

    def refresh_session(self) -> None:
        try:
            grant = self.client.open_chat(self.session_id)
            self.session_token = grant["access_token"]
        except ClientError as exc:
            self.say(f"Error: {exc}")
            if exc.status == 401:
                self.client.token = ""
                self.say("Your sign-in expired; please run the app again.")

    def message(self, text: str) -> None:
        if self.session_id is None:
            self.say("No chat open. Use /new or /open ID first.")
            return
        if len(text) > 1000:
            self.say("Messages can be at most 1000 characters.")
            return
        result = self.client.send(self.session_id, self.session_token, text)
        self.show_log(result["Chat_log"], tail=1)

    def command(self, line: str) -> bool | None:
        name, _, argument = line.partition(" ")
        argument = argument.strip()
        name = name.casefold()
        if name in ("/quit", "/exit"):
            return False
        if name == "/help":
            self.say(HELP)
        elif name == "/new":
            self.say("Starting a chat…")
            grant = self.client.new_chat(argument or self.default_persona)
            self.session_id, self.session_token = grant["session_ID"], grant["access_token"]
            self.say(f"Chat {self.session_id} started.")
            self.show_log(grant["Chat_log"])
        elif name == "/chats":
            chats = self.client.chats(argument)
            if not chats:
                self.say("No chats yet." if not argument else "No chats match.")
            for chat in chats:
                self.say(f"  {chat['session_id']:>8}  {chat_title(chat)}")
        elif name in ("/open", "/delete"):
            if not argument.isdigit():
                self.say(f"Usage: {name} ID")
            elif name == "/open":
                grant = self.client.open_chat(int(argument))
                self.session_id, self.session_token = grant["session_ID"], grant["access_token"]
                self.say(f"Chat {self.session_id} opened.")
                self.show_log(grant["Chat_log"], tail=10)
            elif self.confirm(f"Delete chat {argument} permanently?"):
                self.client.delete_chat(int(argument))
                if self.session_id == int(argument):
                    self.session_id, self.session_token = None, ""
                self.say("Deleted.")
        elif name == "/profile":
            if not argument:
                self.say("Usage: /profile NAME")
            else:
                self.client.set_display_name(argument)
                self.say(f"Display name is now {self.client.display_name}.")
        elif name == "/knowledge":
            self._knowledge_command(argument)
        elif name == "/password":
            self.client.change_password(
                self.ask_secret("Current password: "), self.ask_secret("New password: ")
            )
            self.say("Password changed. Your other devices were signed out.")
        elif name == "/recovery":
            code = self.client.recovery_code(self.ask_secret("Password: "))
            self.say(f"Recovery code (shown once, keep it safe): {code}")
        elif name in ("/logout", "/logout-all"):
            self.client.logout(everywhere=name == "/logout-all")
            self.say("Signed out.")
        elif name == "/delete-account":
            if self.confirm("Permanently delete your account and every chat?"):
                self.client.delete_account(self.ask_secret("Password: "))
                self.say("Account deleted.")
        else:
            self.say("Unknown command. Type /help.")
        return None

    def _knowledge_command(self, argument: str) -> None:
        try:
            parts = shlex.split(argument)
        except ValueError as exc:
            self.say(f"Error: invalid knowledge command: {exc}")
            return
        if not parts or parts == ["list"]:
            sources = self.client.knowledge_sources()
            if not sources:
                self.say("No knowledge sources yet.")
                return
            for source in sources:
                self.say(
                    f"{source['id']:>4}  {source['title']}  "
                    f"[{source['category']}] {source['source_url']}"
                )
            return
        if parts[0] == "delete" and len(parts) == 2 and parts[1].isdigit():
            self.client.delete_knowledge(int(parts[1]))
            self.say("Knowledge source removed.")
            return
        if parts[0] == "add" and len(parts) in {2, 3}:
            path = Path(parts[1]).expanduser()
            if not path.is_file():
                self.say(f"Error: not a file: {path}")
                return
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                self.say(f"Error reading {path}: {exc}")
                return
            if len(content.encode("utf-8")) > 1_000_000:
                self.say("Error: knowledge files must be at most 1 MiB.")
                return
            category = parts[2] if len(parts) == 3 else "general"
            passages = self.client.add_knowledge(
                source_url=path.resolve().as_uri(),
                title=path.stem,
                content=content,
                category=category,
            )
            self.say(f"Indexed {passages} knowledge passage(s).")
            return
        self.say("Usage: /knowledge [list|add FILE [CATEGORY]|delete ID]")

    def confirm(self, question: str) -> bool:
        return self.ask(f"{question} [y/N] ").strip().casefold() == "y"


def resolve_server(url: str, data_file: Path) -> str | None:
    """Pick, validate and trust the server; returns its URL or None."""
    try:
        server_url = chat_app.normalize_server_url(url)
    except ValueError as exc:
        print(f"Invalid server address: {exc}", file=sys.stderr)
        return None
    if not server_url:
        found = chat_app.discover_servers()
        if found:
            server_url = found[0]
            print(f"Found server: {server_url}")
        else:
            try:
                server_url = chat_app.normalize_server_url(input("Server address (HOST:PORT): "))
            except (ValueError, EOFError) as exc:
                print(f"Invalid server address: {exc}", file=sys.stderr)
                return None
    transport.configure_trust_store(data_file.with_name("known_servers.json"))
    try:
        if not transport.confirm_server_on_terminal(server_url):
            print("The server was not trusted; exiting.", file=sys.stderr)
            return None
    except transport.TransportError as exc:
        print(f"Could not verify {server_url}: {exc}", file=sys.stderr)
        return None
    return server_url


def main() -> int:
    parser = argparse.ArgumentParser(description="Account terminal client for the Super Bot server")
    parser.add_argument("--url", default=chat_app.DEFAULT_URL, help="server URL; discovered if omitted")
    parser.add_argument("--data-file", type=Path, default=chat_app.default_data_file(),
                        help="file next to which the trusted-servers list is kept")
    parser.add_argument("--skip-tests", action="store_true", help="disable the optional SUPER_BOT_RUN_TESTS startup checks")
    args = parser.parse_args()
    if not preflight.ensure_tests_pass(args.skip_tests):
        return 1
    server_url = resolve_server(args.url, args.data_file)
    if server_url is None:
        return 1
    try:
        return AccountApp(AccountClient(server_url)).run()
    except KeyboardInterrupt:
        return 0
    finally:
        client_data.close_all()


if __name__ == "__main__":
    raise SystemExit(main())
