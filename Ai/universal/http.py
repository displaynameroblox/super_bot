"""HTTPS JSON/polling binding. Separate entry point from the encrypted chat API."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import time
import uuid

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from . import contracts as c
from .host import Host, ProtocolError
from .security import Limits, Principal
from .store import Store


class RequestBoundary:
    """Limit bytes while streaming, including chunked bodies, before JSON parsing."""
    def __init__(self, app, maximum, requests_per_minute):
        self.app, self.maximum = app, maximum
        self.request_limit = requests_per_minute
        self.bucket, self.count = -1, 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def reject(status, code, message):
            response = JSONResponse(c.ErrorResponse(error=c.Diagnostic(code=code, message=message)).model_dump(), status_code=status)
            await response(scope, receive, send)

        if scope["scheme"] != "https":
            return await reject(400, "tls_required", "SBP requires HTTPS")
        bucket = int(time.monotonic() // 60)
        if bucket != self.bucket:
            self.bucket, self.count = bucket, 0
        self.count += 1
        if self.count > self.request_limit:
            return await reject(429, "rate_limited", "Host request limit reached; retry next minute")
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.maximum:
                return await reject(413, "payload_too_large", "Request body exceeds the configured limit")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if body:
            try:
                value = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                def depth(v, level=0):
                    if level > 24:
                        raise ValueError()
                    if isinstance(v, dict):
                        for item in v.values():
                            depth(item, level + 1)
                    elif isinstance(v, list):
                        for item in v:
                            depth(item, level + 1)
                depth(value)
            except (ValueError, RecursionError, UnicodeDecodeError):
                return await reject(422, "invalid_request", "Body must be bounded, finite JSON")
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def response_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message["headers"]) + [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
            await send(message)
        await self.app(scope, replay, response_send)


def create_app(host: Host, runtime=None) -> FastAPI:
    if runtime is None:
        from .runtime import Runtime
        runtime = Runtime(host)
    errors = {status: {"model": c.ErrorResponse} for status in (400, 401, 403, 404, 409, 413, 422, 426, 429, 500, 502)}
    app = FastAPI(title="Super-Bot Protocol", version="0.1", docs_url=None, redoc_url=None, responses=errors)
    app.state.host, app.state.runtime = host, runtime
    app.add_middleware(RequestBoundary, maximum=host.limits.max_body_bytes,
                       requests_per_minute=host.limits.requests_per_minute * 10)
    bearer = HTTPBearer(auto_error=False, description="Bootstrap credential for POST /sbp/v0.1/sessions; session bearer elsewhere.")

    def token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or len(credentials.credentials) > 512:
            raise ProtocolError("unauthorized", "Bearer credential required", 401)
        return credentials.credentials

    @app.exception_handler(ProtocolError)
    async def protocol_error(_request, exc):
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse(c.ErrorResponse(error=c.Diagnostic(code="invalid_request", message="Request does not match the published schema")).model_dump(), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return JSONResponse(c.ErrorResponse(error=c.Diagnostic(code="http_error", message="Route or method is not available")).model_dump(), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(_request, _exc):
        return JSONResponse(c.ErrorResponse(error=c.Diagnostic(code="internal_error", message="Host could not complete the operation")).model_dump(), status_code=500)

    prefix = "/sbp/v0.1"
    with host.store.transaction():
        node = host.store.get("metadata", "node")
        if node is None:
            node = {"node_id": f"nod_{uuid.uuid4()}"}
            host.store.put("metadata", "node", node)

    @app.get("/.well-known/superbot", response_model=c.NodeDescriptor)
    def descriptor():
        return {**node, "status": "ready", "sdk_versions": [],
                "sbp_versions": c.ProtocolInfo().versions, "sbp_path": prefix,
                "features": ["minecraft.private_chat", "minecraft.movement_gate"]}

    @app.get("/health", response_model=c.Status)
    def health():
        return {"status": "ok"}

    @app.get(prefix + "/protocol", response_model=c.ProtocolInfo)
    def protocol():
        return c.ProtocolInfo()

    @app.get(prefix + "/models", response_model=c.ModelRoutes)
    def models(credential=Depends(token)):
        return host.invoke(credential, "models", registered=tuple(runtime.models))

    movement_path = prefix + "/environments/{environment_id}/agents/{agent_id}/movement-gate"

    @app.get(movement_path, response_model=c.MovementGate)
    def get_movement_gate(environment_id: c.Id, agent_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "movement_gate", environment_id=environment_id, agent_id=agent_id)

    @app.put(movement_path, response_model=c.MovementGate)
    def set_movement_gate(environment_id: c.Id, agent_id: c.Id, req: c.MovementGate, credential=Depends(token)):
        return host.invoke(credential, "movement_gate", environment_id=environment_id, agent_id=agent_id, req=req)

    @app.post(prefix + "/sessions", response_model=c.SessionGrant)
    def session(req: c.SessionRequest, credential=Depends(token)):
        return host.start_session(credential, req)

    @app.post(prefix + "/sessions/reconnect", response_model=c.SessionGrant)
    def reconnect(credential=Depends(token)):
        return host.invoke(credential, "reconnect")

    @app.delete(prefix + "/sessions/{session_id}", response_model=c.Status)
    def revoke_session(session_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "revoke_session", session_id=session_id)

    @app.delete(prefix + "/principals/{principal_id}", response_model=c.Status)
    def revoke_principal(principal_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "revoke_principal", principal_id=principal_id)

    @app.post(prefix + "/environments", response_model=c.Environment)
    def register_environment(req: c.EnvironmentRequest, credential=Depends(token)):
        return host.invoke(credential, "register_environment", req=req)

    @app.get(prefix + "/environments", response_model=list[c.Environment])
    def environments(credential=Depends(token)):
        return host.invoke(credential, "environments")

    @app.post(prefix + "/agents", response_model=c.Agent)
    def register_agent(req: c.AgentRequest, credential=Depends(token)):
        return host.invoke(credential, "register_agent", req=req)

    @app.get(prefix + "/agents", response_model=list[c.Agent])
    def agents(credential=Depends(token)):
        return host.invoke(credential, "agents")

    @app.get(prefix + "/agents/{agent_id}", response_model=c.Agent)
    def agent(agent_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "agent", agent_id=agent_id)

    @app.patch(prefix + "/agents/{agent_id}", response_model=c.Agent)
    def agent_state(agent_id: c.Id, req: c.AgentState, credential=Depends(token)):
        return host.invoke(credential, "set_agent_state", agent_id=agent_id, req=req)

    @app.post(prefix + "/agents/{agent_id}/sessions", response_model=c.SessionGrant)
    def agent_session(agent_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "agent_session", agent_id=agent_id)

    @app.post(prefix + "/agents/{agent_id}/actions", response_model=c.Action)
    def submit_action(agent_id: c.Id, req: c.ActionRequest, credential=Depends(token)):
        return host.invoke(credential, "submit_action", agent_id=agent_id, req=req)

    @app.get(prefix + "/agents/{agent_id}/actions/{action_id}", response_model=c.Action)
    def action(agent_id: c.Id, action_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "action", agent_id=agent_id, action_id=action_id)

    @app.post(prefix + "/agents/{agent_id}/actions/{action_id}/cancel", response_model=c.Action)
    def cancel_action(agent_id: c.Id, action_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "cancel_action", agent_id=agent_id, action_id=action_id)

    @app.get(prefix + "/environments/{environment_id}/actions", response_model=list[c.Action])
    def poll_actions(environment_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "poll_actions", environment_id=environment_id)

    @app.post(prefix + "/environments/{environment_id}/agents/{agent_id}/actions/{action_id}", response_model=c.Action)
    def update_action(environment_id: c.Id, agent_id: c.Id, action_id: c.Id, req: c.ActionUpdate, credential=Depends(token)):
        return host.invoke(credential, "update_action", environment_id=environment_id, agent_id=agent_id, action_id=action_id, req=req)

    @app.post(prefix + "/environments/{environment_id}/observations", response_model=c.Event)
    def observe(environment_id: c.Id, req: c.ObservationRequest, credential=Depends(token)):
        return host.invoke(credential, "observe", environment_id=environment_id, req=req)

    @app.get(prefix + "/agents/{agent_id}/events", response_model=c.EventPage)
    def events(agent_id: c.Id, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), credential=Depends(token)):
        return host.invoke(credential, "events", agent_id=agent_id, after=after, limit=limit)

    @app.post(prefix + "/messages", response_model=c.Event)
    def user_message(req: c.MessageRequest, credential=Depends(token)):
        return host.invoke(credential, "message", req=req)

    @app.post(prefix + "/agents/{agent_id}/messages", response_model=c.Event)
    def agent_message(agent_id: c.Id, req: c.MessageRequest, credential=Depends(token)):
        return host.invoke(credential, "message", sender_id=agent_id, req=req)

    @app.put(prefix + "/agents/{agent_id}/knowledge-sources", response_model=c.Agent)
    def knowledge_policy(agent_id: c.Id, sources: list[c.Source], credential=Depends(token)):
        return host.invoke(credential, "set_knowledge_sources", agent_id=agent_id, sources=sources)

    @app.post(prefix + "/agents/{agent_id}/knowledge", response_model=c.Knowledge)
    def put_knowledge(agent_id: c.Id, req: c.KnowledgeRequest, credential=Depends(token)):
        return host.invoke(credential, "put_knowledge", agent_id=agent_id, req=req)

    @app.get(prefix + "/agents/{agent_id}/knowledge", response_model=list[c.Knowledge])
    def knowledge(agent_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "knowledge", agent_id=agent_id)

    @app.post(prefix + "/agents/{agent_id}/tasks", response_model=c.Task)
    async def run_task(agent_id: c.Id, req: c.TaskRequest, credential=Depends(token)):
        return await runtime.run(credential, agent_id, req)

    @app.get(prefix + "/agents/{agent_id}/tasks", response_model=list[c.Task])
    def tasks(agent_id: c.Id, credential=Depends(token)):
        return host.invoke(credential, "tasks", agent_id=agent_id)

    @app.post(prefix + "/agents/{agent_id}/private-chat", response_model=c.PrivateChatReply)
    async def private_chat(agent_id: c.Id, req: c.PrivateChatRequest, credential=Depends(token)):
        return await runtime.private_chat(credential, agent_id, req)

    @app.post(prefix + "/agents/{agent_id}/private-chat/reset", response_model=c.Status)
    def reset_private_chat(agent_id: c.Id, req: c.PrivateChatScope, credential=Depends(token)):
        return host.invoke(credential, "private_chat_reset", agent_id=agent_id, req=req)

    @app.get(prefix + "/audit", response_model=list[c.AuditEntry])
    def audit(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), credential=Depends(token)):
        return host.invoke(credential, "audit", after=after, limit=limit)

    return app


def load_config(path: Path):
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Host configuration must be private (chmod 600)")
    data = json.loads(path.read_text())
    if set(data) - {"principals", "limits", "existing_model_provider"}:
        raise ValueError("Unknown host configuration key")
    principals = []
    for entry in data["principals"]:
        entry = dict(entry)
        for key in ("environments", "capabilities", "permissions", "knowledge_sources", "models"):
            if key in entry:
                entry[key] = frozenset(entry[key])
        principals.append(Principal(**entry))
    return principals, Limits(**data.get("limits", {})), bool(data.get("existing_model_provider", False))


def main():
    parser = argparse.ArgumentParser(description="Run the independent SBP v0.1 HTTPS host")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--allow-lan", action="store_true")
    args = parser.parse_args()
    try:
        local = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        local = args.host == "localhost"
    if not local and not args.allow_lan:
        parser.error("Use --allow-lan to explicitly enable network access")
    from Ai import tls
    from .runtime import ExistingProvider, Runtime
    import uvicorn
    principals, limits, existing_provider = load_config(args.config)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and args.data_dir.stat().st_mode & 0o077:
        parser.error("Host data directory must be private (chmod 700)")
    material = tls.prepare_server_tls(directory=args.data_dir / "tls", certfile=os.getenv("SSL_CERTFILE", ""),
                                      keyfile=os.getenv("SSL_KEYFILE", ""), keyfile_password=os.getenv("SSL_KEYFILE_PASSWORD", ""))
    host = Host(Store(args.data_dir / "sbp.sqlite3"), principals, limits)
    runtime = Runtime(host)
    if existing_provider:
        # Match the existing chat server's environment loading, but only when
        # the administrator explicitly enables the model bridge.
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
        if os.getenv("SUPER_BOT_MANAGED") != "1":
            load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
        runtime.register_model("existing", ExistingProvider())
    print(f"SBP v0.1 HTTPS on {args.host}:{args.port}; certificate SHA-256: {material.fingerprint}")
    try:
        uvicorn.run(create_app(host, runtime), host=args.host, port=args.port,
                    ssl_certfile=str(material.certfile), ssl_keyfile=str(material.keyfile),
                    ssl_keyfile_password=material.keyfile_password, workers=1, access_log=False,
                    limit_concurrency=128, timeout_keep_alive=5)
    finally:
        host.store.close()


if __name__ == "__main__":
    main()
