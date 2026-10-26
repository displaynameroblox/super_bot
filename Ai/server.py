import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
from datetime import datetime
from itertools import count
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from google import genai
from pydantic import BaseModel, Field, ValidationError
import uvicorn


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

app = FastAPI()
chat_sessions: dict[int, "Chatsession"] = {}
session_ids = count(1)
session_passwords: dict[int, tuple[bytes, bytes] | None] = {}
session_tokens: dict[bytes, int] = {}
session_locks: dict[int, asyncio.Lock] = {}
pending_session_ids: set[int] = set()
DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"SUPER_BOT_DISCOVER_V1"
MAX_MODEL_CHAT_CONTEXT_LENGTH = 12_000
MAX_CODING_CONTEXT_LENGTH = 24_000


def timestamped_message(speaker: str, text: str) -> str:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"[{timestamp}] {speaker}: {text}"


class Chatsession(BaseModel):
    Chat_log: str
    AI_persona: str
    plr_name: str
    session_ID: int


class AssistantResponse(BaseModel):
    reply: str
    is_coding_request: bool


class ChatResponse(Chatsession):
    is_coding_request: bool


class SessionGrant(Chatsession):
    access_token: str


class start_ses_req(BaseModel):
    Chat_log: str = Field(default="", max_length=10000)
    AI_persona: str = Field(min_length=1, max_length=4000)
    plr_name: str = Field(min_length=1, max_length=100)
    session_ID: int | None = Field(default=None, ge=1)
    password: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    session_ID: int = Field(ge=1)
    password: str = Field(default="", max_length=128)


class Chatreg(BaseModel):
    msg: str = Field(min_length=1, max_length=1000)
    ses_id: int = Field(ge=1)
    coding_context: str = Field(default="", max_length=MAX_CODING_CONTEXT_LENGTH)


class SessionEventManager:
    def __init__(self) -> None:
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, session_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(session_id, set()).add(websocket)

    def disconnect(self, session_id: int, websocket: WebSocket) -> None:
        connections = self.connections.get(session_id)
        if connections is None:
            return
        connections.discard(websocket)
        if not connections:
            self.connections.pop(session_id, None)

    async def broadcast(self, session_id: int, event: dict[str, str]) -> None:
        for websocket in tuple(self.connections.get(session_id, ())):
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, OSError, RuntimeError):
                self.disconnect(session_id, websocket)


event_manager = SessionEventManager()


def get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_APT_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_APT_KEY is not configured",
        )
    return genai.Client(api_key=api_key)


def generate_reply(
    client: genai.Client,
    session: Chatsession,
    chat_log: str,
    coding_context: str = "",
    current_message: str = "",
) -> AssistantResponse:
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=(
            f"Player name: {session.plr_name}\n\n"
            f"Recent chat history:\n"
            f"{chat_log[-MAX_MODEL_CHAT_CONTEXT_LENGTH:] or '(No earlier messages.)'}\n\n"
            f"Current user message:\n"
            f"{current_message or '(No new user message.)'}\n\n"
            f"Coding-mode context supplied by the client:\n"
            f"{coding_context or '(No coding skills or coding-mode history.)'}"
        ),
        config={
            "system_instruction": (
                f"{session.AI_persona}\n\n"
                "Reply naturally to the latest user message. Also determine whether "
                "that message asks to create, modify, debug, review, test, or plan "
                "software/code in the current project. Use the conversation and "
                "coding-mode context only to resolve intent and references. "
                "Ordinary chat and coding-related small talk are not coding requests. "
                "Do not execute commands or claim to have changed files. Return the "
                "reply and the coding-request classification in the required schema."
            ),
            "response_mime_type": "application/json",
            "response_schema": AssistantResponse,
        },
    )
    parsed = response.parsed
    if isinstance(parsed, AssistantResponse):
        return parsed
    if isinstance(parsed, dict):
        try:
            return AssistantResponse.model_validate(parsed)
        except ValidationError as exc:
            raise HTTPException(
                status_code=502,
                detail="The AI returned an invalid structured response",
            ) from exc
    if response.text is None:
        raise HTTPException(status_code=502, detail="The AI returned an empty response")
    try:
        return AssistantResponse.model_validate(json.loads(response.text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=502,
            detail="The AI returned an invalid structured response",
        ) from exc


def password_verifier(password: str) -> tuple[bytes, bytes] | None:
    if not password:
        return None
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt, digest


def password_matches(password: str, verifier: tuple[bytes, bytes] | None) -> bool:
    if verifier is None:
        return not password
    salt, expected_digest = verifier
    actual_digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 200_000
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def issue_access_token(session_id: int) -> str:
    token = secrets.token_urlsafe(32)
    session_tokens[hashlib.sha256(token.encode("ascii")).digest()] = session_id
    return token


def authorized_session_id(authorization: str | None) -> int | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return None
    token_digest = hashlib.sha256(token.encode("utf-8")).digest()
    return session_tokens.get(token_digest)


def require_session_access(session_id: int, authorization: str | None) -> None:
    if authorized_session_id(authorization) != session_id:
        raise HTTPException(status_code=401, detail="Valid session login required")


def grant_response(session: Chatsession, access_token: str) -> SessionGrant:
    return SessionGrant(**session.model_dump(), access_token=access_token)


def generate_session_id() -> int:
    while True:
        session_id = next(session_ids)
        if session_id not in chat_sessions and session_id not in pending_session_ids:
            return session_id


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/start_ses", response_model=SessionGrant)
async def start_ses(req: start_ses_req):
    if req.session_ID is not None and (
        req.session_ID in chat_sessions or req.session_ID in pending_session_ids
    ):
        raise HTTPException(status_code=409, detail="Session ID is already in use")

    session_id = req.session_ID if req.session_ID is not None else generate_session_id()
    if session_id in pending_session_ids:
        raise HTTPException(status_code=409, detail="Session ID is already in use")
    pending_session_ids.add(session_id)
    session = Chatsession(
        Chat_log=req.Chat_log,
        AI_persona=req.AI_persona,
        plr_name=req.plr_name,
        session_ID=session_id,
    )
    try:
        assistant_response = await asyncio.to_thread(
            generate_reply, get_client(), session, session.Chat_log
        )
        session.Chat_log = (
            f"{session.Chat_log}\n"
            f"{timestamped_message('AI', assistant_response.reply)}".strip()
        )
        chat_sessions[session_id] = session
        session_passwords[session_id] = password_verifier(req.password or "")
        session_locks[session_id] = asyncio.Lock()
        token = issue_access_token(session_id)
    finally:
        pending_session_ids.discard(session_id)
    await event_manager.broadcast(
        session_id,
        {"type": "joined", "player_name": session.plr_name},
    )
    return grant_response(session, token)


@app.post("/login", response_model=SessionGrant)
def login(req: LoginRequest):
    session = chat_sessions.get(req.session_ID)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    if not password_matches(req.password, session_passwords.get(req.session_ID)):
        raise HTTPException(status_code=401, detail="Incorrect session password")
    return grant_response(session, issue_access_token(req.session_ID))


@app.get("/chat/{ses_id}", response_model=Chatsession)
def get_chat(ses_id: int, authorization: str | None = Header(default=None)):
    require_session_access(ses_id, authorization)
    session = chat_sessions.get(ses_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@app.post("/chat", response_model=ChatResponse)
async def chat(req: Chatreg, authorization: str | None = Header(default=None)):
    require_session_access(req.ses_id, authorization)
    session = chat_sessions.get(req.ses_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")

    async with session_locks[req.ses_id]:
        updated_log = (
            f"{session.Chat_log}\n"
            f"{timestamped_message(session.plr_name, req.msg)}"
        ).strip()
        assistant_response = await asyncio.to_thread(
            generate_reply,
            get_client(),
            session,
            updated_log,
            req.coding_context,
            req.msg,
        )
        session.Chat_log = (
            f"{updated_log}\n"
            f"{timestamped_message('AI', assistant_response.reply)}".strip()
        )
    await event_manager.broadcast(
        req.ses_id,
        {
            "type": "message",
            "player_name": session.plr_name,
            "text": req.msg,
            "chat_log": session.Chat_log,
        },
    )
    return ChatResponse(
        **session.model_dump(),
        is_coding_request=assistant_response.is_coding_request,
    )


@app.websocket("/events/{session_id}")
async def session_events(websocket: WebSocket, session_id: int):
    if authorized_session_id(websocket.headers.get("authorization")) != session_id:
        await websocket.close(code=1008, reason="Valid session login required")
        return
    if session_id not in chat_sessions:
        await websocket.close(code=1008, reason="Chat session not found")
        return

    await event_manager.connect(session_id, websocket)
    await event_manager.broadcast(
        session_id,
        {"type": "joined", "player_name": chat_sessions[session_id].plr_name},
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        event_manager.disconnect(session_id, websocket)


def serve_discovery(port: int, discovery_socket: socket.socket) -> None:
    response = f"SUPER_BOT_SERVER_V1:{port}".encode("ascii")
    try:
        while True:
            try:
                data, address = discovery_socket.recvfrom(256)
            except TimeoutError:
                continue
            if data == DISCOVERY_REQUEST:
                discovery_socket.sendto(response, address)
    finally:
        discovery_socket.close()


def start_server():
    port_value = os.getenv("PORT", os.getenv("port", "8000"))
    try:
        port = int(port_value)
    except ValueError as exc:
        raise ValueError(f"PORT must be an integer, got {port_value!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT must be between 1 and 65535, got {port}")

    discovery_port_value = os.getenv("DISCOVERY_PORT", str(DISCOVERY_PORT))
    try:
        discovery_port = int(discovery_port_value)
    except ValueError as exc:
        raise ValueError(
            f"DISCOVERY_PORT must be an integer, got {discovery_port_value!r}"
        ) from exc
    if not 1 <= discovery_port <= 65535:
        raise ValueError(
            f"DISCOVERY_PORT must be between 1 and 65535, got {discovery_port}"
        )

    host = os.getenv("HOST", "0.0.0.0")
    discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    discovery_socket.bind(("0.0.0.0", discovery_port))
    discovery_socket.settimeout(1)
    discovery_thread = threading.Thread(
        target=serve_discovery,
        args=(port, discovery_socket),
        name="super-bot-discovery",
        daemon=True,
    )
    discovery_thread.start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
