"""Client-owned accounts, history, knowledge and opt-in training data.

Only opaque temporary credentials and bounded context cross the network. The
server's session copy is disposable; opening a local chat recreates it as needed.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import secrets
import threading
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from Ai import accounts, auth, chat_training, database, knowledge, profile_store, session_store


class LocalClient:
    def __init__(self, directory: Path, send, *, watch=None):
        self.directory = directory
        self.db = database.Database(directory / "client.db")
        self.accounts = accounts.AccountService(self.db)
        self.profiles = profile_store.ProfileStore(self.db)
        self.sessions = session_store.SessionStore(self.db)
        with self.db.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS client_messages (session_id INTEGER PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE CASCADE, messages_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS client_exchanges (session_id INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE, exchange_id TEXT, PRIMARY KEY(session_id, exchange_id))")
        self.send = send
        self.watch = watch
        self.remote_accounts: dict[str, str] = {}
        self.active: dict[int, str] = {}
        self.active_logins: dict[int, str] = {}
        self.stops: dict[int, threading.Event] = {}
        self._lock = threading.RLock()
        self._session_locks: dict[int, threading.RLock] = {}
        self._current_account_token = ""

    def library(self, user_id: int) -> Path:
        return self.directory / "knowledge" / f"{user_id}.db"

    def training_database(self, user_id):
        return self.directory / "training" / str(user_id or "guest") / "chat-exchanges.sqlite"

    def _user(self, token):
        user = self.accounts.authenticate(token)
        if user is None:
            raise accounts.AccountError("Please sign in on this device.", 401)
        return user

    def _grant(self, user, token):
        return {"user_id": user.id, "username": user.username,
                "display_name": self.profiles.get(user.id)["display_name"], "access_token": token}

    def _record(self, session_id: int):
        with self.db.read_connection() as db:
            row = db.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
            messages = db.execute("SELECT messages_json FROM client_messages WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return {"session_ID": row["id"], "Chat_log": row["chat_log"], "AI_persona": row["ai_persona"],
                "plr_name": row["plr_name"], "AI_style": row["ai_style"],
                "AI_skills": json.loads(row["ai_skills_json"]),
                "messages": json.loads(messages[0]) if messages else [], "owner": row["owner_user_id"]}

    def _save(self, data, owner=None):
        with self.db.connection() as db:
            self.sessions.save(data["session_ID"], chat_log=data["Chat_log"], ai_persona=data["AI_persona"],
                               plr_name=data["plr_name"], ai_style=data.get("AI_style", ""),
                               ai_skills=data.get("AI_skills", {}), password_verifier=None,
                               host_token_hash=None, owner_user_id=owner, connection=db)
            db.execute("INSERT OR REPLACE INTO client_messages VALUES (?, ?)",
                       (data["session_ID"], json.dumps(data.get("messages", []), ensure_ascii=False)))
            if data.get("exchange_id"):
                db.execute("INSERT OR IGNORE INTO client_exchanges VALUES (?, ?)", (data["session_ID"], data["exchange_id"]))

    @staticmethod
    def _append_exchange(record, message, reply, player_name=None):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        user_text = message.replace("\n", "\n    ")
        reply_text = reply.replace("\n", "\n    ")
        record["Chat_log"] = f"{record['Chat_log']}\n[{stamp}] {player_name or record['plr_name']}: {user_text}\n[{stamp}] AI: {reply_text}".strip()
        record["messages"].extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])

    def _receive_event(self, session_id, event):
        if not isinstance(event, dict) or event.get("type") != "message" or not event.get("exchange_id"):
            return
        if not isinstance(event.get("text"), str) or not isinstance(event.get("reply"), str):
            return
        with self._lock:
            lock = self._session_locks.setdefault(session_id, threading.RLock())
        with lock:
            record = self._record(session_id)
            if record is None:
                return
            with self.db.read_connection() as db:
                seen = db.execute("SELECT 1 FROM client_exchanges WHERE session_id = ? AND exchange_id = ?", (session_id, event["exchange_id"])).fetchone()
            if seen is not None:
                return
            self._append_exchange(record, event["text"], event["reply"], event.get("player_name"))
            record["exchange_id"] = event["exchange_id"]
            self._save(record, record["owner"])

    def _remote_account(self, base_url, local_token, timeout):
        # Real local account passwords never leave the client.
        remote = self.remote_accounts.get(local_token)
        if remote:
            status, _ = self.send(base_url, "GET", "/accounts/me", None, remote, timeout=timeout)
            if status == 200:
                return remote
        status, data = self.send(base_url, "POST", "/accounts/register",
                                 {"username": "client-" + secrets.token_hex(10), "password": secrets.token_urlsafe(32)},
                                 "", timeout=timeout)
        if status != 200:
            raise accounts.AccountError(data.get("detail", "Could not open a temporary login"), status)
        self.remote_accounts[local_token] = data["access_token"]
        return data["access_token"]

    def _watch(self, base_url, session_id, token):
        if self.watch is None or session_id in self.stops:
            return
        stop = threading.Event()
        self.stops[session_id] = stop
        def run():
            try:
                with self.watch(base_url, session_id, token) as stream:
                    while not stop.is_set():
                        self._receive_event(session_id, stream.recv(timeout=0.5))
            except Exception:
                # The next open recreates a lost session from local history.
                pass
            finally:
                with self._lock:
                    if self.stops.get(session_id) is stop:
                        self.stops.pop(session_id, None)
        threading.Thread(target=run, name=f"client-session-{session_id}", daemon=True).start()

    def leave(self, base_url, session_id, timeout=5):
        with self._lock:
            token = self.active.pop(session_id, None)
            self.active_logins.pop(session_id, None)
            stop = self.stops.pop(session_id, None)
        if token:
            with contextlib.suppress(Exception):
                self.send(base_url, "POST", "/sessions/leave", {"session_id": session_id}, token, timeout=timeout)
        if stop:
            stop.set()

    def close(self, base_url):
        for session_id in tuple(self.active):
            self.leave(base_url, session_id)

    def __call__(self, base_url, method, path, body=None, access_token="", *, timeout=500, **kwargs):
        body = body if isinstance(body, dict) else {}
        sid = body.get("ses_id", body.get("session_id", body.get("session_ID")))
        with self._lock:
            lock = self._session_locks.setdefault(sid, threading.RLock()) if isinstance(sid, int) else contextlib.nullcontext()
        try:
            with lock:
                return self._call(base_url, method, path, body, access_token, timeout, **kwargs)
        except (accounts.AccountError, auth.InvalidCredentials, profile_store.ProfileError, ValueError) as exc:
            return getattr(exc, "status", 400), {"detail": str(exc)}

    def _call(self, url, method, path, body, token, timeout, **kwargs):
        if path == "/login":
            record = self._record(body.get("session_ID"))
            if record is not None:
                if record["owner"] is not None:
                    local_token = self._current_account_token
                    user = self._user(local_token)
                    if user.id != record["owner"]:
                        return 404, {"detail": "Chat not found in this local account"}
                    return self._call(url, "POST", "/accounts/chats/open", {"session_id": record["session_ID"]}, local_token, timeout)
                status, result = self.send(url, method, path, body, token, timeout=timeout)
                if status == 404:
                    payload = {k: v for k, v in record.items() if k not in {"owner", "session_ID"}}
                    payload.update(session_ID=record["session_ID"], resume=True,
                                   password=body.get("password", ""), Chat_log=record["Chat_log"][-10000:],
                                   messages=self._context_messages(record["messages"]))
                    status, result = self._call(url, "POST", "/start_ses", payload, "", timeout)
                    if status == 200:
                        result.update(Chat_log=record["Chat_log"], messages=record["messages"])
                        self._save(result)
                if status == 200:
                    result["Chat_log"] = record["Chat_log"]
                    self.active[record["session_ID"]] = result["access_token"]
                    self._watch(url, record["session_ID"], result["access_token"])
                return status, result
            status, result = self.send(url, method, path, body, token, timeout=timeout)
            if status == 200:
                self._save(result)
                self.active[result["session_ID"]] = result["access_token"]
                self._watch(url, result["session_ID"], result["access_token"])
            return status, result
        if path.startswith("/accounts/"):
            route = path.removeprefix("/accounts/")
            if route == "register":
                user = self.accounts.register(body.get("username"), body.get("password"), display_name=body.get("display_name", ""))
                user, issued = self.accounts.login(user.username, body["password"])
                self._current_account_token = issued
                return 200, self._grant(user, issued)
            if route == "login":
                user, issued = self.accounts.login(body.get("username"), body.get("password"))
                self._current_account_token = issued
                return 200, self._grant(user, issued)
            if route == "recover":
                self.accounts.recover(body.get("username"), body.get("recovery_code"), body.get("new_password"))
                self._end_invalid_logins(url)
                return 200, {"ok": True}
            user = self._user(token)
            if route == "me":
                return 200, {"user_id": user.id, "username": user.username, "profile": self.profiles.get(user.id)}
            if route == "profile":
                return 200, self.profiles.update(user.id, **{k: v for k, v in body.items() if k in {"display_name", "avatar", "preferences", "model_settings"}})
            if route == "password":
                self.accounts.change_password(user.id, body.get("current_password"), body.get("new_password"), keep_token=token)
                self._end_invalid_logins(url)
                return 200, {"ok": True}
            if route == "recovery_code":
                code = self.accounts.create_recovery_code(user.id, body.get("password"))
                return 200, {"recovery_code": code, "note": "Shown once. Keep this recovery code private."}
            if route in {"logout", "logout_all", "delete"}:
                if route == "delete":
                    self.accounts.delete_account(user.id, body.get("password"))
                    shutil.rmtree(self.training_database(user.id).parent, ignore_errors=True)
                    for suffix in ("", "-wal", "-shm"):
                        self.library(user.id).with_name(f"{user.id}.db{suffix}").unlink(missing_ok=True)
                elif route == "logout_all":
                    self.accounts.logout_everywhere(user.id)
                else:
                    self.accounts.logout(token)
                self._end_invalid_logins(url)
                return 200, {"ok": True, "sessions_ended": 1}
            if route == "chats":
                return 200, {"chats": self.sessions.list_for_user(user.id, body.get("query", ""))}
            if route.startswith("chats/"):
                sid = body.get("session_id")
                record = self._record(sid)
                if record is None or record["owner"] != user.id:
                    return 404, {"detail": "Chat not found on this device"}
                if route == "chats/get":
                    return 200, {"session_id": sid, "chat_log": record["Chat_log"], "persona": record["AI_persona"], "player_name": record["plr_name"], "style": record["AI_style"]}
                if route == "chats/delete":
                    self.leave(url, sid)
                    self.sessions.delete_for_user(user.id, sid)
                    return 200, {"ok": True}
                if route == "chats/open":
                    existing = self.active.get(sid)
                    if existing:
                        status, result = self.send(url, "GET", f"/chat/{sid}", None, existing, timeout=timeout)
                        if status == 200:
                            self.active_logins[sid] = token
                            result.update(Chat_log=record["Chat_log"], access_token=existing)
                            return status, result
                    remote = self._remote_account(url, token, timeout)
                    status, result = self.send(url, "POST", path, body, remote, timeout=timeout)
                    if status in {401, 404}:
                        payload = {k: v for k, v in record.items() if k not in {"owner", "session_ID"}}
                        payload.update(session_ID=sid, resume=True, Chat_log=record["Chat_log"][-10000:], messages=self._context_messages(record["messages"]))
                        status, result = self.send(url, "POST", "/start_ses", payload, remote, timeout=timeout)
                    if status == 200:
                        result["Chat_log"] = record["Chat_log"]
                        self.active[sid] = result["access_token"]
                        self.active_logins[sid] = token
                        self._watch(url, sid, result["access_token"])
                    return status, result
            if route == "knowledge":
                return 200, {"sources": [s.__dict__ for s in knowledge.list_sources(self.library(user.id))]}
            if route == "knowledge/add":
                allowed = {"source_url", "title", "content", "category", "author", "published_at", "license"}
                if not isinstance(body.get("content"), str) or len(body["content"].encode()) > 1_000_000:
                    raise ValueError("Knowledge documents must be at most 1 MiB")
                return 200, {"ok": True, "passages": knowledge.add_document(self.library(user.id), **{k: v for k, v in body.items() if k in allowed})}
            if route == "knowledge/delete":
                removed = knowledge.delete_source(self.library(user.id), body.get("source_id"))
                return (200, {"ok": True}) if removed else (404, {"detail": "Knowledge source not found"})
            return 404, {"detail": "Unknown local account operation"}

        owner = None
        local_login = token
        if path == "/start_ses" and token:
            owner = self._user(token).id
            token = self._remote_account(url, token, timeout)
        record = self._record(body.get("ses_id")) if path in {"/chat", "/style", "/skills"} else None
        if record and record["owner"] is not None:
            local_login = self.active_logins.get(record["session_ID"])
            if not local_login or self.accounts.authenticate(local_login) is None:
                self.leave(url, record["session_ID"])
                return 401, {"detail": "Please sign in and reopen this local chat"}
        if path == "/chat" and record:
            library = self.library(record["owner"]) if record["owner"] else self.directory / "knowledge.db"
            body = {**body, "knowledge_context": knowledge.format_context(knowledge.search(library, body.get("msg", "")))[:6000]}
        status, result = self.send(url, method, path, body or None, token, timeout=timeout, **kwargs)
        if status != 200 or not isinstance(result, dict):
            return status, result
        if path == "/start_ses" and "session_ID" in result:
            self._save(result, owner)
            self.active[result["session_ID"]] = result["access_token"]
            if owner is not None:
                self.active_logins[result["session_ID"]] = local_login
            self._watch(url, result["session_ID"], result["access_token"])
        elif path == "/chat" and record:
            messages = record["messages"]
            reply_messages = result.get("messages", [])
            reply = result.get("reply")
            if not isinstance(reply, str) and reply_messages and reply_messages[-1].get("role") == "assistant":
                reply = reply_messages[-1]["content"]
            if isinstance(reply, str):
                self._append_exchange(record, body["msg"], reply)
                result["Chat_log"] = record["Chat_log"]
                result["messages"] = messages
                if body.get("learn_from_chats"):
                    chat_training.save_exchange(self.training_database(record["owner"]), body["msg"], reply)
            else:
                # A malformed response must never replace the durable transcript.
                return 502, {"detail": "The model reply was missing. Saved history was not changed."}
            self._save(result, record["owner"])
        elif path in {"/style", "/skills"} and record:
            record.update(result)
            self._save(record, record["owner"])
        return status, result

    @staticmethod
    def _context_messages(messages):
        result = []
        remaining = 24000
        for message in reversed(messages[-100:]):
            if remaining <= 0:
                break
            content = message["content"][-remaining:]
            result.append({"role": message["role"], "content": content})
            remaining -= len(content)
        return list(reversed(result))

    def _end_invalid_logins(self, url):
        for local, remote in tuple(self.remote_accounts.items()):
            if self.accounts.authenticate(local) is None:
                with contextlib.suppress(Exception):
                    self.send(url, "POST", "/accounts/logout", {}, remote, timeout=5)
                self.remote_accounts.pop(local, None)
        # Server invalidates the derived chat tokens when the temporary login ends.


_clients: dict[tuple[str, str], LocalClient] = {}
_clients_lock = threading.Lock()


def local_history(base_url, session_id, event=None):
    """Read the full client transcript, applying a live exchange exactly once."""
    from Ai import transport
    address = transport.parse_server_url(base_url).key
    root = transport.trust_store().path.parent / "client-data"
    with _clients_lock:
        client = _clients.get((str(root), address))
    if client is None:
        return None
    if event is not None:
        client._receive_event(session_id, event)
    record = client._record(session_id)
    return record["Chat_log"] if record else None


def directory_for_url(base_url: str) -> Path:
    from Ai import transport
    address = transport.parse_server_url(base_url).key
    root = transport.trust_store().path.parent / "client-data"
    return root / hashlib.sha256(address.encode()).hexdigest()[:24]


def request(base_url, method, path, body=None, access_token="", *, timeout=500, **kwargs):
    from Ai import transport
    # Leave non-data transport probes and malformed calls to the transport.
    if not (path.startswith("/accounts/") or path in {"/start_ses", "/login"} or (path in {"/chat", "/style", "/skills"} and isinstance(body, dict) and "ses_id" in body)):
        return transport.secure_request(base_url, method, path, body, access_token, timeout=timeout, **kwargs)
    address = transport.parse_server_url(base_url).key
    root = transport.trust_store().path.parent / "client-data"
    key = (str(root), address)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            directory = directory_for_url(base_url)
            client = LocalClient(directory, lambda *a, **kw: transport.secure_request(*a, **kw), watch=transport.open_event_stream)
            _clients[key] = client
    try:
        return client(base_url, method, path, body, access_token, timeout=timeout, **kwargs)
    except (OSError, database.DatabaseError) as exc:
        raise transport.TransportError(f"Could not save local user data: {exc}") from exc


def close_all():
    for (_, address), client in tuple(_clients.items()):
        client.close(f"https://{address}")


atexit.register(close_all)
