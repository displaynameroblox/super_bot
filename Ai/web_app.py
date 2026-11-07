"""Client-side web app: a local browser UI that talks to a Super Bot server.

This runs on the *client* computer, not on the server. It serves the static page
from ``Ai/web/`` on loopback and forwards the browser's JSON calls to the server
through ``backend`` (the encrypted, fingerprint-pinned transport). The browser never
sees API keys or chat-session tokens; the account token is held in an HttpOnly,
SameSite=Strict cookie that only the local web app can read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")

# (method, path, json body, bearer token) -> (HTTP status, JSON body)
Backend = Callable[[str, str, Any, str | None], Awaitable[tuple[int, Any]]]

WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
COOKIE_NAME = "sb_account"
COOKIE_MAX_AGE = 7 * 24 * 3600
CSRF_HEADER = "x-super-bot-web"
MAX_BODY_BYTES = 64 * 1024
DEFAULT_PERSONA = "A friendly, helpful assistant."
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
GRANT_ROUTES = {"register", "login"}
PUBLIC_ROUTES = {"recover"}
PASSTHROUGH_ROUTES = {
    "logout",
    "logout_all",
    "password",
    "delete",
    "recovery_code",
    "profile",
    "chats",
    "chats/get",
    "chats/delete",
    "knowledge",
    "knowledge/add",
    "knowledge/delete",
}
ENDS_ACCOUNT_SESSION = {"logout", "logout_all", "delete"}


def _json(status: int, body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=SECURITY_HEADERS)


def build_web_app(backend: Backend, port: int) -> FastAPI:
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    # Chat-session tokens stay here, keyed by (account token, chat id); never sent to the browser.
    chat_tokens: dict[tuple[str, int], str] = {}

    def reply(status: int, body: Any, *, token: str | None = None, clear: bool = False) -> JSONResponse:
        response = _json(status, body)
        if token:
            response.set_cookie(
                COOKIE_NAME, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict", path="/"
            )
        if clear:
            response.delete_cookie(COOKIE_NAME, path="/")
        return response

    def forget_account(token: str) -> None:
        for key in [key for key in chat_tokens if key[0] == token]:
            chat_tokens.pop(key, None)

    @web.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "").casefold() not in allowed_hosts:
            return _json(403, {"detail": "Unexpected Host header"})
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin.casefold().removeprefix("http://") not in allowed_hosts:
                return _json(403, {"detail": "Cross-origin requests are refused"})
            if request.headers.get(CSRF_HEADER) != "1":
                return _json(403, {"detail": "Missing request header"})
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def read_json(request: Request) -> dict[str, Any] | None:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return None
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_BODY_BYTES:
                return None
        if not data:
            return {}
        try:
            parsed = json.loads(data)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def open_chat(token: str, session_id: int) -> tuple[int, Any]:
        status, data = await backend("POST", "/accounts/chats/open", {"session_id": session_id}, token)
        if status == 200 and isinstance(data, dict) and data.get("access_token"):
            chat_tokens[(token, session_id)] = data["access_token"]
        return status, data

    async def create_session(body: dict[str, Any], token: str) -> JSONResponse:
        persona = body.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            persona = DEFAULT_PERSONA
        style = body.get("style")
        status, me = await backend("GET", "/accounts/me", None, token)
        if status != 200:
            return reply(status, me, clear=status == 401)
        name = me["profile"].get("display_name") or me["username"]
        status, data = await backend(
            "POST",
            "/start_ses",
            {
                "AI_persona": persona.strip()[:4000],
                "AI_style": style.strip() if isinstance(style, str) else "",
                "plr_name": name[:100],
            },
            token,
        )
        if status == 200 and isinstance(data, dict):
            access_token = data.pop("access_token", None)
            if access_token:
                chat_tokens[(token, data["session_ID"])] = access_token
        return reply(status, data, clear=status == 401)

    async def send_message(body: dict[str, Any], token: str) -> JSONResponse:
        session_id, message = body.get("session_id"), body.get("message")
        if (
            not isinstance(session_id, int)
            or isinstance(session_id, bool)
            or not isinstance(message, str)
            or not message.strip()
        ):
            return reply(400, {"detail": "session_id and message are required"})
        payload = {"ses_id": session_id, "msg": message.strip()[:1000], "show_progress": False}
        for attempt in (1, 2):
            chat_token = chat_tokens.get((token, session_id))
            if chat_token is None:
                status, data = await open_chat(token, session_id)
                if status != 200:
                    return reply(404 if status == 404 else status, {"detail": data.get("detail", "Chat not found")}
                                 if isinstance(data, dict) else {"detail": "Chat not found"},
                                 clear=status == 401)
                chat_token = chat_tokens[(token, session_id)]
            status, data = await backend("POST", "/chat", payload, chat_token)
            if status == 401 and attempt == 1:
                chat_tokens.pop((token, session_id), None)
                continue
            return reply(status, data)
        return reply(401, {"detail": "Please sign in"})

    @web.api_route("/api/{route:path}", methods=["GET", "POST"])
    async def api(route: str, request: Request):
        body = await read_json(request)
        if body is None:
            return reply(400, {"detail": "Invalid or oversized JSON body"})

        if request.method == "POST" and route in GRANT_ROUTES:
            status, data = await backend("POST", f"/accounts/{route}", body, None)
            if status == 200 and isinstance(data, dict):
                return reply(status, data, token=data.pop("access_token", None))
            return reply(status, data)
        if request.method == "POST" and route in PUBLIC_ROUTES:
            status, data = await backend("POST", f"/accounts/{route}", body, None)
            return reply(status, data)

        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return reply(401, {"detail": "Please sign in"})

        if route == "me" and request.method == "GET":
            status, data = await backend("GET", "/accounts/me", None, token)
            return reply(status, data, clear=status == 401)
        if route == "knowledge" and request.method == "GET":
            status, data = await backend("GET", "/accounts/knowledge", None, token)
            return reply(status, data, clear=status == 401)
        if request.method != "POST":
            return reply(404, {"detail": "Not Found"})
        if route == "leave":
            for (account, sid), chat_token in tuple(chat_tokens.items()):
                if account == token:
                    await backend("POST", "/sessions/leave", {"session_id": sid}, chat_token)
                    chat_tokens.pop((account, sid), None)
            return reply(200, {"ok": True})
        if route == "sessions":
            return await create_session(body, token)
        if route == "send":
            return await send_message(body, token)
        if route in PASSTHROUGH_ROUTES:
            status, data = await backend("POST", f"/accounts/{route}", body, token)
            if status == 200 and route == "chats/delete":
                chat_tokens.pop((token, body.get("session_id")), None)
            ended = (status == 200 and route in ENDS_ACCOUNT_SESSION) or status == 401
            if ended:
                forget_account(token)
            return reply(status, data, clear=ended)
        return reply(404, {"detail": "Not Found"})

    @web.get("/{asset:path}")
    async def static(asset: str):
        entry = STATIC_FILES.get("/" + asset)
        if entry is None:
            return _json(404, {"detail": "Not Found"})
        name, media_type = entry
        try:
            content = (WEB_DIRECTORY / name).read_bytes()
        except OSError:
            return _json(404, {"detail": "Web client files are missing"})
        return Response(content, media_type=media_type, headers=SECURITY_HEADERS)

    return web
