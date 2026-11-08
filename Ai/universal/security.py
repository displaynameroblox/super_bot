"""Administrator-owned grants; adapters never grant themselves permissions."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import secrets
from .contracts import SOURCES


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    id: str
    token_hash: str
    role: str  # admin, controller, adapter
    environments: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    knowledge_sources: frozenset[str] = field(default_factory=lambda: frozenset(SOURCES))
    models: frozenset[str] = frozenset()

    def allows(self, category: str, value: str) -> bool:
        values = getattr(self, category)
        return self.role == "admin" or value in values or "*" in values


@dataclass(frozen=True)
class Context:
    principal: Principal
    session_id: str
    agent_id: str | None = None


@dataclass(frozen=True)
class Limits:
    session_seconds: int = 3600
    requests_per_minute: int = 600
    max_sessions: int = 32
    max_agents: int = 64
    max_environments: int = 16
    max_actions_per_agent: int = 1000
    max_events_per_agent: int = 2000
    max_knowledge_per_agent: int = 256
    max_tasks_per_agent: int = 256
    max_body_bytes: int = 65536
    audit_entries: int = 10000
    inference_concurrency: int = 4

    def __post_init__(self):
        if any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in vars(self).values()):
            raise ValueError("Limits must be positive integers")
