"""Public entry point of the session server: HTTPS only and end-to-end encrypted.

The network can reach /health, the server identity and the encrypted endpoints
and nothing else. The real API lives in the inner app, which is called
in-process with requests that were decrypted here, so it never answers a plain
request, and a relay in front of the server cannot see which endpoint is used.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from Ai import e2ee
from Ai.protocol import E2EE_EVENTS_PATH, E2EE_IDENTITY_PATH, E2EE_PATH


logger = logging.getLogger("uvicorn.error")

ALLOWED_ROUTES = (
    ("POST", re.compile(r"/start_ses")),
    ("POST", re.compile(r"/login")),
    ("POST", re.compile(r"/chat")),
    ("POST", re.compile(r"/style")),
    ("POST", re.compile(r"/skills")),
    ("POST", re.compile(r"/sessions/leave")),
    ("GET", re.compile(r"/chat/[0-9]{1,18}")),
    (
        "POST",
        re.compile(
            r"/accounts/(register|login|logout|logout_all|password|delete|recovery_code"
            r"|recover|profile|chats|chats/get|chats/open|chats/delete|knowledge"
            r"|knowledge/add|knowledge/delete)"
        ),
    ),
    ("GET", re.compile(r"/accounts/(me|knowledge)")),
)
EVENTS_PATH = re.compile(r"/events/([0-9]{1,18})")
HELLO_TIMEOUT_SECONDS = 10
MAX_AUTHORIZATION_LENGTH = 512

AccessCheck = Callable[[int, str | None], tuple[int, str] | None]
EventStreamer = Callable[[Any, int, str | None], Awaitable[None]]


@dataclass(frozen=True)
class _Call:
    method: str
    path: str
    authorization: str | None
    body: Any


def _parse_call(payload: Any) -> _Call | None:
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    path = payload.get("path")
    authorization = payload.get("authorization")
    if not isinstance(method, str) or not isinstance(path, str):
        return None
    if authorization is not None and (
        not isinstance(authorization, str)
        or not authorization.isascii()
        or len(authorization) > MAX_AUTHORIZATION_LENGTH
    ):
        return None
    return _Call(method.upper(), path, authorization, payload.get("body"))


async def _read_body(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_size = int(declared_length)
        except ValueError as exc:
            raise e2ee.InvalidEnvelope("Malformed Content-Length") from exc
        if declared_size > e2ee.MAX_REQUEST_BYTES:
            raise e2ee.EnvelopeTooLarge("The request is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > e2ee.MAX_REQUEST_BYTES:
            raise e2ee.EnvelopeTooLarge("The request is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _envelope_error(error: e2ee.E2EEError) -> JSONResponse:
    if isinstance(error, e2ee.ReplayedRequest):
        status = 409
    elif isinstance(error, e2ee.EnvelopeTooLarge):
        status = 413
    else:
        status = 400
    body: dict[str, Any] = {"error": error.code}
    if isinstance(error, e2ee.StaleRequest):
        body["server_time"] = error.server_time
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


async def _dispatch(inner_app: Any, call: _Call) -> tuple[int, Any]:
    if not any(
        call.method == method and pattern.fullmatch(call.path)
        for method, pattern in ALLOWED_ROUTES
    ):
        return 404, {"detail": "Not Found"}
    headers = {"Authorization": call.authorization} if call.authorization else {}
    json_body = call.body if call.method != "GET" else None
    transport = httpx.ASGITransport(app=inner_app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://session.internal",
            timeout=None,
        ) as client:
            response = await client.request(
                call.method,
                call.path,
                json=json_body,
                headers=headers,
            )
    except Exception:
        logger.exception("Unhandled error in %s %s", call.method, call.path)
        return 500, {"detail": "Internal Server Error"}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"detail": "The server returned a non-JSON response"}


class EncryptedEventSocket:
    """Looks like a WebSocket to SessionEventManager but seals every event."""

    def __init__(self, websocket: WebSocket, exchange: e2ee.ServerExchange) -> None:
        self._websocket = websocket
        self._exchange = exchange
        self._encryptor: e2ee.StreamEncryptor | None = None
        self._lock = asyncio.Lock()

    async def accept(self) -> None:
        async with self._lock:
            await self._websocket.send_bytes(
                self._exchange.seal_response({"status": 200, "body": {}})
            )
            self._encryptor = self._exchange.stream_encryptor()

    async def send_json(self, event: dict[str, str]) -> None:
        async with self._lock:
            if self._encryptor is None:
                raise RuntimeError("The encrypted event stream is not open")
            await self._websocket.send_bytes(self._encryptor.seal(event))

    async def close(self, code: int = 1000) -> None:
        await self._websocket.close(code=code)

    async def receive_text(self) -> str:
        while True:
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))


def build_gateway(
    inner_app: Any,
    identity: e2ee.ServerIdentity,
    *,
    access_error: AccessCheck,
    stream_events: EventStreamer,
    replay_cache: e2ee.ReplayCache | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    gateway = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=inner_app.router.lifespan_context)
    replays = replay_cache or e2ee.ReplayCache()
    public_key = base64.urlsafe_b64encode(identity.public_key).decode("ascii")

    build_marker = Path(__file__).with_name(".release-build")
    build = build_marker.read_text(encoding="ascii").strip() if build_marker.is_file() else ""
    build = os.getenv("SUPER_BOT_RELEASE_BUILD", build)
    startup_id = os.getenv("SUPER_BOT_STARTUP_ID", "")

    @gateway.get("/health")
    def health() -> dict[str, str]:
        if build and startup_id:
            return {"status": "ok", "build": build, "startup_id": startup_id}
        return {"status": "ok"}

    @gateway.get(E2EE_IDENTITY_PATH)
    def server_identity() -> dict[str, Any]:
        return {"version": e2ee.PROTOCOL_VERSION, "public_key": public_key}

    @gateway.post(E2EE_PATH)
    async def encrypted_call(request: Request) -> Response:
        try:
            data = await _read_body(request)
            exchange = e2ee.open_request(
                identity, data, replay_cache=replays, now=clock()
            )
        except e2ee.E2EEError as exc:
            return _envelope_error(exc)
        call = _parse_call(exchange.payload)
        if call is None:
            status, body = 400, {"detail": "Malformed encrypted request"}
        else:
            status, body = await _dispatch(inner_app, call)
        return Response(
            exchange.seal_response({"status": status, "body": body}),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @gateway.websocket(E2EE_EVENTS_PATH)
    async def encrypted_events(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            message = await _receive_hello(websocket)
            if message is None:
                return
            exchange = e2ee.open_request(
                identity, message, replay_cache=replays, now=clock()
            )
        except (asyncio.TimeoutError, TimeoutError, e2ee.E2EEError):
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1008)
            return

        payload = exchange.payload if isinstance(exchange.payload, dict) else {}
        path = payload.get("path")
        authorization = payload.get("authorization")
        match = EVENTS_PATH.fullmatch(path) if isinstance(path, str) else None
        if match is None or not (
            authorization is None
            or (isinstance(authorization, str) and len(authorization) <= MAX_AUTHORIZATION_LENGTH)
        ):
            await _reject(websocket, exchange, 404, "Not Found")
            return
        session_id = int(match.group(1))
        error = access_error(session_id, authorization)
        if error is not None:
            await _reject(websocket, exchange, *error)
            return
        await stream_events(
            EncryptedEventSocket(websocket, exchange), session_id, authorization
        )

    return gateway


async def _receive_hello(websocket: WebSocket) -> bytes | None:
    message = await asyncio.wait_for(websocket.receive(), HELLO_TIMEOUT_SECONDS)
    if message["type"] == "websocket.disconnect":
        return None
    data = message.get("bytes")
    if data is None:
        raise e2ee.InvalidEnvelope("Expected a binary hello frame")
    return data


async def _reject(
    websocket: WebSocket,
    exchange: e2ee.ServerExchange,
    status: int,
    detail: str,
) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_bytes(
            exchange.seal_response({"status": status, "body": {"detail": detail}})
        )
        await websocket.close(code=1008)
