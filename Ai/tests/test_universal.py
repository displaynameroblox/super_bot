"""Universal core acceptance and authorization regression tests; no cloud needed."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json

import pytest
from pydantic import ValidationError

from Ai.universal import contracts as c
from Ai.universal.host import Host, ProtocolError
from Ai.universal.security import Limits, Principal, digest
from Ai.universal.store import Store


CAP = c.Capability(id="navigation.move_to", version="1", cancellation=True, permissions=["move"],
                   input_schema={"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"], "additionalProperties": False},
                   output_schema={"type": "object", "properties": {"arrived": {"type": "boolean"}}, "required": ["arrived"], "additionalProperties": False})


def principals():
    return [Principal("admin", digest("admin-secret"), "admin"),
            Principal("game-sdk", digest("game-secret"), "adapter", frozenset({"game"}), frozenset({CAP.id})),
            Principal("robot-sdk", digest("robot-secret"), "adapter", frozenset({"robot"}), frozenset({CAP.id})),
            Principal("alice", digest("alice-secret"), "controller", frozenset({"game", "robot"}), frozenset({CAP.id}),
                      frozenset({"move", "knowledge.share:b", "message:b"}), models=frozenset({"test"})),
            Principal("bob", digest("bob-secret"), "controller", frozenset({"game"}), frozenset({CAP.id}), frozenset({"move"}))]


class World:
    def __init__(self, path, limits=None):
        self.path = path
        self.now = 1000.0
        self.host = Host(Store(path), principals(), limits, clock=lambda: self.now)
        self.tokens = {p.id: self.host.start_session(secret, c.SessionRequest())["access_token"] for p, secret in zip(principals(),
                       ["admin-secret", "game-secret", "robot-secret", "alice-secret", "bob-secret"])}
        for env, owner in [("game", "game-sdk"), ("robot", "robot-sdk")]:
            self.call(owner, "register_environment", req=c.EnvironmentRequest(id=env, name=env, kind=env, capabilities=[CAP]))
        self.call("alice", "register_agent", req=c.AgentRequest(id="a", name="A", environments=["game", "robot"], capabilities=[CAP.id]))
        self.call("bob", "register_agent", req=c.AgentRequest(id="b", name="B", environments=["game"], capabilities=[CAP.id]))

    def call(self, who, op, **kwargs):
        return self.host.invoke(self.tokens.get(who, who), op, **kwargs)

    def action(self, id="one", agent="a", owner="alice", env="game"):
        return self.call(owner, "submit_action", agent_id=agent,
                         req=c.ActionRequest(id=id, environment_id=env, capability=CAP.id, version="1", input={"x": 1}))

    def update(self, state, **kwargs):
        return self.call("game-sdk", "update_action", environment_id="game", agent_id="a", action_id="one",
                         req=c.ActionUpdate(state=state, **kwargs))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "sbp.db")
    yield w
    w.host.store.close()


def error(code, fn):
    with pytest.raises(ProtocolError) as caught:
        fn()
    assert caught.value.body["error"]["code"] == code


def test_engine_independent_registration_and_standalone_agent(world):
    assert len(world.call("alice", "environments")) == 2
    assert [e["id"] for e in world.call("bob", "environments")] == ["game"]
    agent = world.call("alice", "register_agent", req=c.AgentRequest(id="standalone", name="Standalone"))
    assert agent["environments"] == []
    assert world.action(env="robot")["environment_id"] == "robot"
    error("forbidden", lambda: world.call("game-sdk", "poll_actions", environment_id="robot"))


def test_identity_reconnect_rotation_expiration_and_restart(world):
    old = world.tokens["game-sdk"]
    grant = world.call("game-sdk", "reconnect")
    error("unauthorized", lambda: world.host.invoke(old, "environments"))
    world.tokens["game-sdk"] = grant["access_token"]
    world.host.store.close()
    world.host = Host(Store(world.path), principals(), clock=lambda: world.now)
    assert world.call("game-sdk", "environments")[0]["id"] == "game"
    assert world.call("game-sdk", "reconnect")["session_id"] == grant["session_id"]
    world.now += 4000
    error("unauthorized", lambda: world.call("alice", "agents"))
    world.tokens["alice"] = world.host.start_session("alice-secret", c.SessionRequest())["access_token"]
    assert world.call("alice", "agents")[0]["id"] == "a"
    assert b"alice-secret" not in world.path.read_bytes()


def test_exact_version_negotiation_and_no_schema_network_references(world):
    error("version_mismatch", lambda: world.host.start_session("alice-secret", c.SessionRequest(versions=["2"])))
    with pytest.raises(ValidationError):
        c.Capability(id="x", version="1", input_schema={"$ref": "http://localhost/secret"}, output_schema={})
    with pytest.raises(ValidationError):
        c.ActionRequest(id="x", environment_id="game", capability="x", version="1", unexpected=True)


def test_action_lifecycle_validation_idempotency(world):
    first = world.action()
    assert first == world.action()
    error("invalid_transition", lambda: world.update("completed", result={"arrived": True}))
    world.update("acknowledged")
    world.update("running", progress=0.5)
    error("invalid_progress", lambda: world.update("running", progress=0.1))
    error("schema_violation", lambda: world.update("completed", progress=1.0, result={"arrived": "yes"}))
    done = world.update("completed", progress=1.0, result={"arrived": True})
    assert done == world.update("completed", progress=1.0, result={"arrived": True})
    assert world.call("game-sdk", "poll_actions", environment_id="game") == []
    error("conflict", lambda: world.call("alice", "submit_action", agent_id="a", req=c.ActionRequest(
        id="one", environment_id="game", capability=CAP.id, version="1", input={"x": 2})))


def test_unsupported_denied_and_malformed_actions(world):
    for cap, value, code in [("unknown", {}, "unsupported_capability"), (CAP.id, {"x": "bad"}, "schema_violation")]:
        error(code, lambda: world.call("alice", "submit_action", agent_id="a", req=c.ActionRequest(
            id="bad", environment_id="game", capability=cap, version="1", input=value)))
    error("forbidden", lambda: world.action(agent="b"))
    world.host.principals["alice"] = replace(world.host.principals["alice"], permissions=frozenset())
    error("forbidden", lambda: world.action())


def test_cancel_timeout_and_revocation_before_delivery(world):
    world.action()
    assert world.call("alice", "cancel_action", agent_id="a", action_id="one")["state"] == "cancelled"
    world.action("two")
    world.now += 61
    assert world.call("alice", "action", agent_id="a", action_id="two")["error"]["code"] == "deadline_exceeded"
    world.action("three")
    world.call("admin", "revoke_principal", principal_id="alice")
    assert world.call("game-sdk", "poll_actions", environment_id="game") == []
    error("unauthorized", lambda: world.call("alice", "agents"))


def test_running_cancel_handshake_and_output_isolation(world):
    world.action()
    world.update("acknowledged")
    assert world.call("alice", "cancel_action", agent_id="a", action_id="one")["state"] == "cancel_requested"
    assert world.call("game-sdk", "poll_actions", environment_id="game")[0]["state"] == "cancel_requested"
    error("forbidden", lambda: world.call("robot-sdk", "update_action", environment_id="robot", agent_id="a", action_id="one",
                                          req=c.ActionUpdate(state="cancelled")))
    assert world.update("cancelled")["state"] == "cancelled"


def test_parallel_submissions_are_atomic_and_bounded(world):
    with ThreadPoolExecutor(max_workers=8) as pool:
        duplicates = list(pool.map(lambda _: world.action(), range(16)))
    assert all(d["id"] == "one" for d in duplicates)
    assert len(world.call("game-sdk", "poll_actions", environment_id="game")) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: world.action(str(n)), range(7)))
    error("quota_exceeded", lambda: world.action("overflow"))
    assert len(world.call("game-sdk", "poll_actions", environment_id="game")) == 8


def test_observations_events_and_agent_bound_credentials(world):
    token = world.call("alice", "agent_session", agent_id="a")["access_token"]
    req = c.ObservationRequest(id="obs1", agent_id="a", kind="sensor.sample", data={"distance": 2}, observed_at=10.0)
    event = world.call("robot-sdk", "observe", environment_id="robot", req=req)
    assert event == world.call("robot-sdk", "observe", environment_id="robot", req=req)
    assert world.call(token, "events", agent_id="a")["events"][0] == event
    error("forbidden", lambda: world.call(token, "agent", agent_id="b"))
    error("forbidden", lambda: world.call(token, "agent_session", agent_id="a"))
    error("forbidden", lambda: world.call("game-sdk", "events", agent_id="a"))


def test_knowledge_provenance_isolation_claims_and_source_policy(world):
    private = world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(
        source="user", content="A user claim", provenance="conversation:1"))
    assert private["verified"] is False
    assert world.call("bob", "knowledge", agent_id="b") == []
    error("unverified_claim", lambda: world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(
        source="user", content="claim", provenance="user", verified=True)))
    world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(
        source="experience", content="Shared experience", provenance="observation:1", scope="shared", shared_with=["b"]))
    assert len(world.call("bob", "knowledge", agent_id="b")) == 1
    world.call("bob", "set_knowledge_sources", agent_id="b", sources=["memory"])
    assert world.call("bob", "knowledge", agent_id="b") == []
    assert world.call("alice", "knowledge", agent_id="a")[0]["owner"] == "alice"


def test_messages_and_denial_audit_without_secrets(world):
    event = world.call("alice", "message", req=c.MessageRequest(recipient="b", text="hello"), sender_id="a")
    assert event["data"]["sender"] == "a"
    error("forbidden", lambda: world.call("bob", "message", req=c.MessageRequest(recipient="a", text="unauthorized")))
    rows = world.call("admin", "audit")
    assert rows[-1]["outcome"] == "forbidden"
    assert "hello" not in json.dumps(rows)
    assert "secret" not in json.dumps(rows)


def test_rate_and_retention_limits(world):
    world.host.limits = replace(world.host.limits, requests_per_minute=1, max_events_per_agent=2)
    world.now += 60
    world.call("alice", "agents")
    error("rate_limited", lambda: world.call("alice", "agents"))
    world.host.limits = replace(world.host.limits, requests_per_minute=600)
    for i in range(3):
        world.call("alice", "message", req=c.MessageRequest(recipient="b", text=str(i)))
    error("cursor_expired", lambda: world.call("bob", "events", agent_id="b", after=0))
    assert len(world.call("bob", "events", agent_id="b", after=1)["events"]) == 2


class ASGITransport:
    def __init__(self, app):
        import httpx
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://localhost")

    async def request(self, method, path, *, token=None, body=None):
        from Ai.universal.adapter import RemoteError
        response = await self.client.request(method, path, headers={"Authorization": "Bearer " + token} if token else {}, json=body)
        if not response.is_success:
            raise RemoteError(response.status_code, response.json())
        return response.json()


def test_http_two_mock_environments_observations_and_restart_replay(world, tmp_path):
    import asyncio
    from Ai.universal.http import create_app
    from Ai.universal.mock import MockAdapter
    async def scenario():
        transport = ASGITransport(create_app(world.host))
        game = MockAdapter(transport, environment_id="game", kind="game", ledger=tmp_path / "game.db", clock=lambda: world.now)
        robot = MockAdapter(transport, environment_id="robot", kind="robotics", ledger=tmp_path / "robot.db", clock=lambda: world.now)
        try:
            await asyncio.gather(game.connect("game-secret"), robot.connect("robot-secret"))
            world.action("game-action", env="game")
            world.action("robot-action", env="robot")
            await asyncio.gather(game.run_once(), robot.run_once())
            assert len(await game.poll()) == 1
            await game.reconnect()
            await asyncio.gather(game.run_once(), robot.run_once())
            for name in ("game-action", "robot-action"):
                assert world.call("alice", "action", agent_id="a", action_id=name)["state"] == "completed"
            assert game.executions == robot.executions == 1
            await game.observe(c.ObservationRequest(id="sample", agent_id="a", kind="position", data={"x": 1}, observed_at=world.now))
            assert any(e["type"] == "observation" for e in world.call("alice", "events", agent_id="a")["events"])
            assert await game.run_once() == 0
        finally:
            game.close()
            robot.close()
            await transport.client.aclose()
    asyncio.run(scenario())


def test_http_auth_validation_tls_and_bounded_bodies(world):
    from fastapi.testclient import TestClient
    from Ai.universal.http import create_app
    app = create_app(world.host)
    with TestClient(app, base_url="https://localhost") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/sbp/v0.1/agents").status_code == 401
        auth = {"Authorization": "Bearer " + world.tokens["alice"]}
        response = client.post("/sbp/v0.1/agents", json={"id": "x", "name": "secret-body", "unknown": True}, headers=auth)
        assert response.status_code == 422
        assert "secret-body" not in response.text
        assert client.post("/sbp/v0.1/agents", content=b"x" * 65537, headers=auth).status_code == 413
        assert client.post("/sbp/v0.1/agents", content=b'{"x":NaN}', headers=auth).status_code == 422
        assert client.get("/missing").json()["error"]["code"] == "http_error"
        assert client.get("/sbp/v0.1/agents", headers=auth).headers["cache-control"] == "no-store"
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/health").json()["error"]["code"] == "tls_required"


def test_http_agent_token_and_environment_cannot_access_other_resources(world):
    from fastapi.testclient import TestClient
    from Ai.universal.http import create_app
    with TestClient(create_app(world.host), base_url="https://localhost") as client:
        auth = {"Authorization": "Bearer " + world.tokens["alice"]}
        token = client.post("/sbp/v0.1/agents/a/sessions", headers=auth).json()["access_token"]
        bound = {"Authorization": "Bearer " + token}
        assert client.get("/sbp/v0.1/agents/b/knowledge", headers=bound).status_code == 403
        assert client.post("/sbp/v0.1/agents", headers=bound, json={"id": "intruder", "name": "Intruder"}).status_code == 403
        assert client.get("/sbp/v0.1/audit", headers=auth).status_code == 403
        assert client.post("/sbp/v0.1/environments/robot/observations", headers=auth, json={
            "id": "obs", "agent_id": "a", "kind": "x", "data": {}, "observed_at": 0.0}).status_code == 403


def test_runtime_concurrent_agents_context_and_explicit_plans(world):
    import asyncio
    from Ai.universal.runtime import Runtime
    class Provider:
        def __init__(self):
            self.active = self.maximum = 0
            self.contexts = []
        async def generate(self, messages):
            self.contexts.append(json.loads(messages[-1]["content"]))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return "Plan ready"
    runtime, provider = Runtime(world.host), Provider()
    runtime.register_model("test", provider)
    world.host.principals["bob"] = replace(world.host.principals["bob"], models=frozenset({"test"}))
    world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(source="memory", content="private A", provenance="test"))
    world.call("game-sdk", "observe", environment_id="game", req=c.ObservationRequest(id="obs", agent_id="a", kind="sensor", data={"x": 1}, observed_at=1.0))
    async def scenario():
        a, b = await asyncio.gather(
            runtime.run(world.tokens["alice"], "a", c.TaskRequest(goal="Go", model="test", actions=[c.ActionRequest(
                id="task-action", environment_id="game", capability=CAP.id, version="1", input={"x": 1})])),
            runtime.run(world.tokens["bob"], "b", c.TaskRequest(goal="Think", model="test")))
        assert a["state"] == "waiting" and b["state"] == "completed"
        assert a["action_ids"] == ["task-action"]
    asyncio.run(scenario())
    assert provider.maximum == 2
    assert len(provider.contexts[0]["knowledge"]) == 1
    assert provider.contexts[0]["events"][0]["type"] == "observation"
    assert provider.contexts[1]["knowledge"] == []


def test_runtime_plan_failure_is_atomic_and_sanitizes_provider_errors(world):
    import asyncio
    from Ai.universal.runtime import Runtime
    class FailingProvider:
        async def generate(self, messages):
            raise RuntimeError("SECRET_API_KEY")
    runtime = Runtime(world.host)
    runtime.register_model("test", FailingProvider())
    async def scenario():
        with pytest.raises(ProtocolError) as caught:
            await runtime.run(world.tokens["alice"], "a", c.TaskRequest(goal="Fail", model="test"))
        assert "SECRET" not in str(caught.value)
        good = c.ActionRequest(id="plan-good", environment_id="game", capability=CAP.id, version="1", input={"x": 1})
        bad = c.ActionRequest(id="plan-bad", environment_id="game", capability="unsupported", version="1")
        with pytest.raises(ProtocolError):
            await runtime.run(world.tokens["alice"], "a", c.TaskRequest(goal="Atomic plan", actions=[good, bad]))
    asyncio.run(scenario())
    assert world.call("game-sdk", "poll_actions", environment_id="game") == []
    assert all(t["state"] == "failed" for t in world.call("alice", "tasks", agent_id="a"))


def test_mock_independent_local_safety_stop(world):
    import asyncio
    from Ai.universal.http import create_app
    from Ai.universal.mock import MockAdapter
    async def scenario():
        transport = ASGITransport(create_app(world.host))
        adapter = MockAdapter(transport, environment_id="game", kind="game", clock=lambda: world.now)
        try:
            await adapter.connect("game-secret")
            world.action()
            await adapter.run_once()
            adapter.local_stop = True
            await adapter.run_once()
            assert adapter.executions == 0
            assert world.call("alice", "action", agent_id="a", action_id="one")["error"]["code"] == "native_execution_failed"
        finally:
            adapter.close()
            await transport.client.aclose()
    asyncio.run(scenario())


def test_custom_capability_and_version_requires_no_core_change(world):
    capability = c.Capability(id="custom.inspect", version="2026", input_schema={"type": "object"}, output_schema={"type": "string"})
    world.host.principals["game-sdk"] = replace(world.host.principals["game-sdk"], capabilities=frozenset({capability.id}))
    world.host.principals["alice"] = replace(world.host.principals["alice"], capabilities=frozenset({capability.id}))
    world.call("game-sdk", "register_environment", req=c.EnvironmentRequest(id="game", name="Game", kind="game", capabilities=[capability]))
    world.call("alice", "register_agent", req=c.AgentRequest(id="custom", name="Custom", environments=["game"], capabilities=[capability.id]))
    result = world.call("alice", "submit_action", agent_id="custom", req=c.ActionRequest(id="inspect", environment_id="game", capability=capability.id, version="2026"))
    assert result["state"] == "queued"


def test_openapi_all_operations_have_typed_responses_and_auth(world):
    from Ai.universal.http import create_app
    spec = create_app(world.host).openapi()
    public = {"/health", "/sbp/v0.1/protocol"}
    for path, methods in spec["paths"].items():
        for operation in methods.values():
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]
            if path not in public:
                assert operation["security"] == [{"HTTPBearer": []}]
    assert "Roblox" not in json.dumps(spec)


def test_mock_replays_durable_result_after_lost_response_and_adapter_restart(world, tmp_path):
    import asyncio
    import httpx
    from Ai.universal.http import create_app
    from Ai.universal.mock import MockAdapter
    class Interrupted(ASGITransport):
        fail = True
        async def request(self, method, path, *, token=None, body=None):
            if body and body.get("state") == "completed" and self.fail:
                self.fail = False
                raise httpx.ConnectError("simulated disconnection before result delivery")
            return await super().request(method, path, token=token, body=body)
    async def scenario():
        transport = Interrupted(create_app(world.host))
        ledger = tmp_path / "durable-results.db"
        adapter = MockAdapter(transport, environment_id="game", kind="game", ledger=ledger, clock=lambda: world.now)
        try:
            await adapter.connect("game-secret")
            world.action()
            await adapter.run_once()
            with pytest.raises(httpx.ConnectError):
                await adapter.run_once()
            assert adapter.executions == 1
            adapter.close()
            adapter = MockAdapter(transport, environment_id="game", kind="game", ledger=ledger, clock=lambda: world.now)
            await adapter.connect("game-secret")
            await adapter.run_once()
            assert adapter.executions == 0
            assert world.call("alice", "action", agent_id="a", action_id="one")["state"] == "completed"
        finally:
            adapter.close()
            await transport.client.aclose()
    asyncio.run(scenario())


def test_environment_revocation_filters_existing_evidence(world):
    world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(
        source="environment", scope="environment", environment_id="robot", content="Sensor map", provenance="sensor"))
    world.call("robot-sdk", "observe", environment_id="robot", req=c.ObservationRequest(
        id="robot-obs", agent_id="a", kind="sensor", data={"private": True}, observed_at=1.0))
    world.host.principals["alice"] = replace(world.host.principals["alice"], environments=frozenset({"game"}))
    assert world.call("alice", "knowledge", agent_id="a") == []
    page = world.call("alice", "events", agent_id="a")
    assert page["events"] == [] and page["next_cursor"] == 1
    assert world.call("alice", "context", agent_id="a")["events"] == []


def test_runtime_revocation_during_inference_prevents_action_dispatch(world):
    import asyncio
    from Ai.universal.runtime import Runtime
    class RevokingProvider:
        async def generate(self, messages):
            world.call("admin", "revoke_principal", principal_id="alice")
            return "Proceed"
    runtime = Runtime(world.host)
    runtime.register_model("test", RevokingProvider())
    async def scenario():
        with pytest.raises(ProtocolError):
            await runtime.run(world.tokens["alice"], "a", c.TaskRequest(goal="Go", model="test", actions=[c.ActionRequest(
                id="revoked", environment_id="game", capability=CAP.id, version="1", input={"x": 1})]))
    asyncio.run(scenario())
    assert world.call("game-sdk", "poll_actions", environment_id="game") == []
    assert world.call("admin", "tasks", agent_id="a")[0]["state"] == "failed"


def test_real_https_two_environment_demo():
    from Ai.universal.demo import run_demo
    result = run_demo()
    assert result["actions"] == ["completed", "completed"]
    assert result["reconnected_session_preserved"]
    assert result["observation_received"]
    assert result["cloud_services"] == 0


def test_wire_artifacts_match_implementation_and_examples(world):
    from Ai.universal.http import create_app
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[2] / "docs" / "sbp"
    spec = json.loads((root / "openapi.json").read_text())
    assert spec == create_app(world.host).openapi()
    schemas = json.loads((root / "schemas.json").read_text())
    Draft202012Validator.check_schema(schemas)
    for example in json.loads((root / "examples.json").read_text()):
        # Paths in examples use concrete IDs; locate the corresponding OpenAPI route.
        import re
        path = next(p for p in spec["paths"] if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", p), example["path"]))
        operation = spec["paths"][path][example["method"].lower()]
        if example["request"] is not None:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            Draft202012Validator(schema | {"components": spec["components"]}).validate(example["request"])
        schema = operation["responses"][str(example["status"])]["content"]["application/json"]["schema"]
        Draft202012Validator(schema | {"components": spec["components"]}).validate(example["response"])


def test_pending_actions_memory_and_revocation_persist_after_restart(world):
    world.action()
    world.call("alice", "put_knowledge", agent_id="a", req=c.KnowledgeRequest(source="memory", content="Remember", provenance="test"))
    world.call("admin", "revoke_principal", principal_id="bob")
    task = world.call("alice", "create_task", agent_id="a", req=c.TaskRequest(goal="Interrupted"))
    world.host.store.close()
    world.host = Host(Store(world.path), principals(), clock=lambda: world.now)
    assert world.call("game-sdk", "poll_actions", environment_id="game")[0]["id"] == "one"
    assert world.call("alice", "knowledge", agent_id="a")[0]["content"] == "Remember"
    assert world.call("alice", "tasks", agent_id="a")[0]["error"]["code"] == "host_restarted"
    error("unauthorized", lambda: world.call("bob", "agents"))


def test_knowledge_administrator_ceiling_and_agent_lifecycle(world):
    world.host.principals["alice"] = replace(world.host.principals["alice"], knowledge_sources=frozenset({"memory"}))
    error("forbidden", lambda: world.call("alice", "set_knowledge_sources", agent_id="a", sources=["user"]))
    world.action()
    world.call("alice", "set_agent_state", agent_id="a", req=c.AgentState(status="paused"))
    assert world.call("game-sdk", "poll_actions", environment_id="game") == []
    error("agent_inactive", lambda: world.action("paused"))
    world.call("alice", "set_agent_state", agent_id="a", req=c.AgentState(status="active"))
    assert world.action("resumed")["state"] == "queued"
