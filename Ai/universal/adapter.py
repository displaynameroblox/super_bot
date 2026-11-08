"""Transport interface and minimal SDK base. Native execution belongs in adapters."""
from __future__ import annotations
import ssl
from typing import Protocol, Callable
from jsonschema import Draft202012Validator
from urllib.parse import urlsplit
import httpx
from . import contracts as c

PREFIX = "/sbp/v0.1"


class Transport(Protocol):
    async def request(self, method: str, path: str, *, token: str | None = None, body=None): ...


class RemoteError(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("error", {}).get("code", "remote_error"))
        self.status, self.body = status, body


class HTTPTransport:
    def __init__(self, url: str, *, ca_file: str | None = None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Use an HTTPS host origin without credentials, path, query or fragment")
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), verify=verify, trust_env=False,
                                        follow_redirects=False, timeout=30)

    async def request(self, method, path, *, token=None, body=None):
        if not path.startswith(PREFIX + "/") or "://" in path or ".." in path:
            raise ValueError("Invalid SBP path")
        response = await self.client.request(method, path, headers={"Authorization": f"Bearer {token}"} if token else {}, json=body)
        try:
            value = response.json()
        except ValueError:
            raise RemoteError(response.status_code, {"error": {"code": "invalid_response"}}) from None
        if not response.is_success:
            raise RemoteError(response.status_code, value)
        return value

    async def close(self):
        await self.client.aclose()


class Adapter:
    def __init__(self, transport: Transport, environment: c.EnvironmentRequest):
        self.transport, self.environment = transport, environment
        self.session: c.SessionGrant | None = None

    @property
    def token(self):
        if self.session is None:
            raise RuntimeError("Connect before using the adapter")
        return self.session.access_token

    async def connect(self, credential: str):
        self.session = c.SessionGrant.model_validate(await self.transport.request(
            "POST", PREFIX + "/sessions", token=credential, body=c.SessionRequest().model_dump()))
        await self.transport.request("POST", PREFIX + "/environments", token=self.token, body=self.environment.model_dump())
        return self.session

    async def reconnect(self):
        self.session = c.SessionGrant.model_validate(await self.transport.request("POST", PREFIX + "/sessions/reconnect", token=self.token))
        return self.session

    async def register_agent(self, req: c.AgentRequest):
        return c.Agent.model_validate(await self.transport.request("POST", PREFIX + "/agents", token=self.token, body=req.model_dump()))

    async def observe(self, req: c.ObservationRequest):
        return c.Event.model_validate(await self.transport.request("POST", f"{PREFIX}/environments/{self.environment.id}/observations",
                                                                  token=self.token, body=req.model_dump()))

    async def poll(self):
        rows = await self.transport.request("GET", f"{PREFIX}/environments/{self.environment.id}/actions", token=self.token)
        return [c.Action.model_validate(row) for row in rows]

    async def update(self, action: c.Action, req: c.ActionUpdate):
        return c.Action.model_validate(await self.transport.request("POST",
            f"{PREFIX}/environments/{self.environment.id}/agents/{action.agent_id}/actions/{action.id}", token=self.token, body=req.model_dump()))


class CapabilityRegistry:
    """SDK-local handlers: custom native actions require no host changes."""
    def __init__(self):
        self.entries: dict[tuple[str, str], tuple[c.Capability, Callable]] = {}

    def register(self, capability: c.Capability, handler: Callable):
        key = capability.id, capability.version
        if key in self.entries:
            raise ValueError("Handler already registered")
        self.entries[key] = capability, handler

    def execute(self, action: c.Action):
        entry = self.entries.get((action.capability, action.version))
        if entry is None:
            raise ValueError("Unsupported capability")
        capability, handler = entry
        if not Draft202012Validator(capability.input_schema).is_valid(action.input):
            raise ValueError("Invalid native action input")
        result = handler(action.input)
        if not Draft202012Validator(capability.output_schema).is_valid(result):
            raise ValueError("Invalid native action output")
        return result


