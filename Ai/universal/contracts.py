"""SBP v0.1 wire contracts; also the source for the generated OpenAPI schemas."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from jsonschema import Draft202012Validator

Id = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=8000)]
Source = Literal["environment", "experience", "developer", "user", "shared", "memory"]
SOURCES = ["environment", "experience", "developer", "user", "shared", "memory"]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Diagnostic(Wire):
    code: Id
    message: str
    retryable: bool = False


class ErrorResponse(Wire):
    error: Diagnostic


class SessionRequest(Wire):
    versions: list[str] = Field(default_factory=lambda: ["0.1"], min_length=1, max_length=8)


class SessionGrant(Wire):
    session_id: Id
    version: Literal["0.1"] = "0.1"
    access_token: str
    expires_at: float
    principal_id: Id
    agent_id: Id | None = None


class Constraints(Wire):
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_concurrent: int = Field(default=8, ge=1, le=128)


# Deliberately bounded, non-resolving JSON Schema profile. No network references,
# regular expressions or combinators whose validation cost is unbounded.
SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
               "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
               "description", "title", "default", "$schema"}


def check_schema(value: dict) -> dict:
    import json
    if len(json.dumps(value)) > 8192:
        raise ValueError("Capability schema exceeds 8192 characters")

    def walk(node, depth=0):
        if depth > 12 or not isinstance(node, (dict, bool)):
            raise ValueError("Invalid or too deeply nested schema")
        if isinstance(node, bool):
            return
        if set(node) - SCHEMA_KEYS:
            raise ValueError("Unsupported JSON Schema keyword")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("Only JSON Schema 2020-12 is supported")
        for child in node.get("properties", {}).values():
            walk(child, depth + 1)
        for key in ("items", "additionalProperties"):
            if key in node:
                walk(node[key], depth + 1)
    try:
        Draft202012Validator.check_schema(value)
        walk(value)
    except Exception as exc:
        raise ValueError("Invalid or unsupported capability schema") from exc
    return value


class Capability(Wire):
    id: Id
    version: Id
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    permissions: list[Id] = Field(default_factory=list, max_length=32)
    constraints: Constraints = Field(default_factory=Constraints)
    status: Literal["available", "unavailable"] = "available"
    cancellation: bool = False

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_schema(cls, value):
        return check_schema(value)


class EnvironmentRequest(Wire):
    id: Id
    name: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Id
    capabilities: list[Capability] = Field(default_factory=list, max_length=64)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value):
        if len({c.id for c in value}) != len(value):
            raise ValueError("Capability identifiers must be unique")
        return value


class Environment(EnvironmentRequest):
    owner: Id


class AgentRequest(Wire):
    id: Id
    name: Annotated[str, Field(min_length=1, max_length=128)]
    environments: list[Id] = Field(default_factory=list, max_length=32)
    capabilities: list[Id] = Field(default_factory=list, max_length=64)
    knowledge_sources: list[Source] = Field(default_factory=lambda: list(SOURCES), max_length=6)


class Agent(AgentRequest):
    owner: Id
    status: Literal["active", "paused", "stopped"] = "active"


class AgentState(Wire):
    status: Literal["active", "paused", "stopped"]


class ActionRequest(Wire):
    id: Id  # durable idempotency key, scoped to this agent
    environment_id: Id
    capability: Id
    version: Id
    input: dict[str, JsonValue] = Field(default_factory=dict)


class Action(ActionRequest):
    agent_id: Id
    state: Literal["queued", "acknowledged", "running", "cancel_requested", "completed", "failed", "cancelled"] = "queued"
    progress: float = Field(default=0.0, ge=0, le=1)
    result: JsonValue = None
    error: Diagnostic | None = None
    created_at: float
    deadline: float
    capability_snapshot: Capability


class ActionUpdate(Wire):
    state: Literal["acknowledged", "running", "completed", "failed", "cancelled"]
    progress: float = Field(default=0.0, ge=0, le=1)
    result: JsonValue = None
    error: Diagnostic | None = None


class ObservationRequest(Wire):
    id: Id
    agent_id: Id
    kind: Id
    data: dict[str, JsonValue]
    observed_at: float


class Event(Wire):
    sequence: int
    type: Id
    agent_id: Id
    environment_id: Id | None = None
    time: float
    data: dict[str, JsonValue]


class EventPage(Wire):
    events: list[Event]
    next_cursor: int


class MessageRequest(Wire):
    recipient: Id
    text: Text


class KnowledgeRequest(Wire):
    source: Source
    content: Text
    provenance: Text
    confidence: float = Field(default=0.5, ge=0, le=1)
    verified: bool = False
    scope: Literal["private", "shared", "environment"] = "private"
    environment_id: Id | None = None
    shared_with: list[Id] = Field(default_factory=list, max_length=32)


class Knowledge(KnowledgeRequest):
    id: Id
    owner: Id
    agent_id: Id
    created_at: float


class TaskRequest(Wire):
    goal: Text
    model: Id | None = None
    actions: list[ActionRequest] = Field(default_factory=list, max_length=16)


class Task(Wire):
    id: Id
    agent_id: Id
    goal: Text
    state: Literal["running", "waiting", "completed", "failed"]
    action_ids: list[Id] = Field(default_factory=list)
    response: str | None = None
    error: Diagnostic | None = None


class ProtocolInfo(Wire):
    versions: list[str] = Field(default_factory=lambda: ["0.1"])
    transports: list[str] = Field(default_factory=lambda: ["https-json-polling"])


class NodeDescriptor(Wire):
    node_id: Id
    status: Literal["ready"]
    sdk_versions: list[str]
    sbp_versions: list[str]
    sbp_path: Literal["/sbp/v0.1"]
    features: list[str] = Field(default_factory=list)


MinecraftUuid = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class PrivateChatScope(Wire):
    environment_id: Id
    world_id: MinecraftUuid
    player_id: MinecraftUuid


class PrivateChatRequest(PrivateChatScope):
    request_id: MinecraftUuid
    model: Id
    text: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]


class PrivateChatReply(Wire):
    conversation_id: Id
    text: Text


class ModelRoutes(Wire):
    routes: list[Id]


class AuditEntry(Wire):
    sequence: int
    time: float
    principal_id: str
    operation: str
    resource: str
    outcome: str


class Status(Wire):
    status: str
