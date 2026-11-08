"""Transport-independent SBP operations and authorization boundaries."""
from __future__ import annotations

import hmac
import json
import time
import uuid
from jsonschema import Draft202012Validator

from . import contracts as c
from .security import Context, Limits, Principal, digest, new_token
from .store import Store

TERMINAL = {"completed", "failed", "cancelled"}
OPERATIONS = {
    "context", "reconnect", "revoke_session", "revoke_principal", "register_environment", "environments",
    "register_agent", "agents", "agent", "set_agent_state", "agent_session", "submit_action",
    "action", "cancel_action", "poll_actions", "update_action", "observe", "events", "message",
    "put_knowledge", "knowledge", "set_knowledge_sources", "audit", "create_task", "finish_task", "tasks",
}


class ProtocolError(Exception):
    def __init__(self, code, message, status=400, retryable=False):
        super().__init__(message)
        self.status = status
        self.body = c.ErrorResponse(error=c.Diagnostic(code=code, message=message, retryable=retryable)).model_dump()


def fail(code, message, status=400):
    raise ProtocolError(code, message, status)


class Host:
    def __init__(self, store: Store, principals: list[Principal], limits: Limits | None = None, *, clock=time.time):
        self.store, self.limits, self.clock = store, limits or Limits(), clock
        if len({p.id for p in principals}) != len(principals):
            raise ValueError("Duplicate principal IDs")
        if len({p.token_hash for p in principals}) != len(principals):
            raise ValueError("Credentials must be unique")
        for p in principals:
            if p.role not in {"admin", "controller", "adapter"} or len(p.token_hash) != 64:
                raise ValueError("Invalid principal configuration")
            c.AgentRequest(id=p.id, name=p.id)
        self.principals = {p.id: p for p in principals}
        # In-flight inference cannot safely be replayed after process death.
        with self.store.transaction():
            for task in self.store.all("task"):
                if task["state"] == "running":
                    task.update(state="failed", error=c.Diagnostic(code="host_restarted", message="Retry with a new task.").model_dump())
                    self.store.put("task", task["id"], task)

    def _audit_log(self, principal, operation, resource="", outcome="ok"):
        db = self.store.db
        db.execute("INSERT INTO audit(time,principal_id,operation,resource,outcome) VALUES(?,?,?,?,?)",
                   (self.clock(), principal, operation, resource[:96], outcome))
        db.execute("DELETE FROM audit WHERE sequence <= (SELECT COALESCE(MAX(sequence),0) FROM audit)-?",
                   (self.limits.audit_entries,))

    def _principal(self, key):
        p = self.principals.get(key)
        if not p or self.store.get("revoked", key):
            fail("unauthorized", "Credential is invalid, expired or revoked", 401)
        return p

    def _authenticate(self, token):
        session = self.store.get("session", digest(token))
        if not session or session["expires_at"] <= self.clock():
            fail("unauthorized", "Credential is invalid, expired or revoked", 401)
        p = self._principal(session["principal_id"])
        return Context(p, session["session_id"], session["agent_id"])

    def _rate(self, p):
        bucket = int(self.clock() // 60)
        rate = self.store.get("rate", p.id) or {"bucket": bucket, "count": 0}
        if rate["bucket"] != bucket:
            rate = {"bucket": bucket, "count": 0}
        if rate["count"] >= self.limits.requests_per_minute:
            raise ProtocolError("rate_limited", "Request quota exceeded; retry next minute", 429, True)
        rate["count"] += 1
        self.store.put("rate", p.id, rate)

    def start_session(self, credential: str, req: c.SessionRequest):
        with self.store.transaction():
            p = next((p for p in self.principals.values() if hmac.compare_digest(p.token_hash, digest(credential))), None)
            if not p:
                self._audit_log("anonymous", "session.create", outcome="unauthorized")
                # Raise outside the transaction so the denial audit is retained.
                result = None
            else:
                self._principal(p.id)
                self._rate(p)
                if "0.1" not in req.versions:
                    fail("version_mismatch", "Supported protocol versions: 0.1", 426)
                result = self._issue_session(p)
                self._audit_log(p.id, "session.create")
        if result is None:
            fail("unauthorized", "Invalid credential", 401)
        return result

    def _issue_session(self, p, agent_id=None, session_id=None):
        now = self.clock()
        for row in self.store.all("session"):
            if row["expires_at"] <= now:
                self.store.delete("session", row["digest"])
        if sum(s["principal_id"] == p.id for s in self.store.all("session")) >= self.limits.max_sessions:
            fail("quota_exceeded", "Session quota exceeded", 429)
        token = new_token()
        grant = c.SessionGrant(session_id=session_id or str(uuid.uuid4()), access_token=token,
                               expires_at=float(now + self.limits.session_seconds), principal_id=p.id, agent_id=agent_id)
        stored = grant.model_dump(exclude={"access_token"}) | {"digest": digest(token)}
        self.store.put("session", digest(token), stored)
        return grant.model_dump()

    def invoke(self, token: str, operation: str, **kwargs):
        """Shared entry for HTTP, in-process transports, and future authenticated transports."""
        if operation not in OPERATIONS:
            fail("unsupported_operation", "Unsupported operation", 404)
        error, result, actor, saved = None, None, "anonymous", False
        with self.store.transaction():
            try:
                ctx = self._authenticate(token)
                actor = ctx.principal.id
                self._rate(ctx.principal)
                self.store.db.execute("SAVEPOINT command")
                saved = True
                result = getattr(self, "_" + operation)(ctx, **kwargs)
            except ProtocolError as exc:
                if saved:
                    self.store.db.execute("ROLLBACK TO command")
                error = exc
            finally:
                if saved:
                    self.store.db.execute("RELEASE command")
            self._audit_log(actor, operation, str(kwargs.get("agent_id", kwargs.get("environment_id", ""))),
                        error.body["error"]["code"] if error else "ok")
        if error:
            raise error
        return result

    def _require_role(self, ctx, *roles):
        if ctx.agent_id or ctx.principal.role not in roles:
            fail("forbidden", "This credential cannot perform the operation", 403)

    def _get(self, kind, key):
        item = self.store.get(kind, key)
        if item is None:
            fail("not_found", "Resource not found", 404)
        return item

    def _env_access(self, ctx, environment_id, *, execute=False):
        if not ctx.principal.allows("environments", environment_id):
            fail("forbidden", "Environment is outside credential scope", 403)
        env = self._get("environment", environment_id)
        if ctx.agent_id and environment_id not in self._agent(ctx, ctx.agent_id)["environments"]:
            fail("forbidden", "Agent is not bound to this environment", 403)
        if execute:
            self._require_role(ctx, "adapter")
            if env["owner"] != ctx.principal.id:
                fail("forbidden", "Only the registered adapter may execute actions", 403)
        return env

    def _agent(self, ctx, agent_id):
        if ctx.agent_id and ctx.agent_id != agent_id:
            fail("forbidden", "Credential is bound to another agent", 403)
        agent = self._get("agent", agent_id)
        if ctx.principal.role != "admin" and agent["owner"] != ctx.principal.id:
            fail("forbidden", "Agent belongs to another principal", 403)
        return agent

    def _quota(self, kind, field, value, maximum):
        if sum(row.get(field) == value for row in self.store.all(kind)) >= maximum:
            fail("quota_exceeded", f"{kind} storage quota exceeded", 429)

    def _event(self, agent_id, type_, data, environment_id=None):
        counter = self.store.get("sequence", agent_id) or {"value": 0}
        counter["value"] += 1
        self.store.put("sequence", agent_id, counter)
        seq = counter["value"]
        event = c.Event(sequence=seq, type=type_, agent_id=agent_id, environment_id=environment_id,
                        time=float(self.clock()), data=data).model_dump()
        self.store.put("event", f"{agent_id}/{seq:020}", event)
        old = seq - self.limits.max_events_per_agent
        if old > 0:
            self.store.delete("event", f"{agent_id}/{old:020}")
        return event

    def _reconnect(self, ctx):
        old = next(s for s in self.store.all("session") if s["session_id"] == ctx.session_id)
        self.store.delete("session", old["digest"])
        return self._issue_session(ctx.principal, ctx.agent_id, ctx.session_id)

    def _revoke_session(self, ctx, session_id):
        row = next((s for s in self.store.all("session") if s["session_id"] == session_id), None)
        if row and (ctx.principal.role == "admin" and not ctx.agent_id or row["session_id"] == ctx.session_id):
            self.store.delete("session", row["digest"])
        else:
            fail("forbidden", "May revoke only own session", 403)
        return {"status": "revoked"}

    def _revoke_principal(self, ctx, principal_id):
        self._require_role(ctx, "admin")
        self._principal(principal_id)
        self.store.put("revoked", principal_id, {"revoked": True})
        return {"status": "revoked"}

    def _register_environment(self, ctx, req: c.EnvironmentRequest):
        self._require_role(ctx, "adapter")
        if not ctx.principal.allows("environments", req.id):
            fail("forbidden", "Environment is outside credential scope", 403)
        for cap in req.capabilities:
            if not ctx.principal.allows("capabilities", cap.id):
                fail("forbidden", "Capability advertisement is outside credential scope", 403)
        old = self.store.get("environment", req.id)
        if old and old["owner"] != ctx.principal.id:
            fail("conflict", "Environment ID already registered", 409)
        if not old:
            self._quota("environment", "owner", ctx.principal.id, self.limits.max_environments)
        env = c.Environment(**req.model_dump(), owner=ctx.principal.id).model_dump()
        self.store.put("environment", req.id, env)
        return env

    def _environments(self, ctx):
        return [e for e in self.store.all("environment") if ctx.principal.allows("environments", e["id"])
                and (not ctx.agent_id or e["id"] in self._agent(ctx, ctx.agent_id)["environments"])]

    def _register_agent(self, ctx, req: c.AgentRequest):
        self._require_role(ctx, "controller", "adapter", "admin")
        for env in req.environments:
            self._env_access(ctx, env)
        if any(not ctx.principal.allows("capabilities", cap) for cap in req.capabilities):
            fail("forbidden", "Agent capabilities exceed administrator grants", 403)
        if any(not ctx.principal.allows("knowledge_sources", src) for src in req.knowledge_sources):
            fail("forbidden", "Knowledge policy exceeds administrator grants", 403)
        old = self.store.get("agent", req.id)
        if old:
            self._agent(ctx, req.id)
            if any(old[k] != v for k, v in req.model_dump().items()):
                fail("conflict", "Agent ID already has different configuration", 409)
            return old
        self._quota("agent", "owner", ctx.principal.id, self.limits.max_agents)
        agent = c.Agent(**req.model_dump(), owner=ctx.principal.id).model_dump()
        self.store.put("agent", req.id, agent)
        return agent

    def _agents(self, ctx):
        return [a for a in self.store.all("agent") if (ctx.principal.role == "admin" or a["owner"] == ctx.principal.id)
                and (not ctx.agent_id or a["id"] == ctx.agent_id)]

    def _set_agent_state(self, ctx, agent_id, req: c.AgentState):
        agent = self._agent(ctx, agent_id)
        agent["status"] = req.status
        self.store.put("agent", agent_id, agent)
        self._event(agent_id, "agent.state", {"status": req.status})
        return agent

    def _agent_session(self, ctx, agent_id):
        self._require_role(ctx, "controller", "adapter", "admin")
        agent = self._agent(ctx, agent_id)
        return self._issue_session(self._principal(agent["owner"]), agent_id)

    def _authorize_action(self, agent, environment_id, cap):
        owner = self._principal(agent["owner"])
        if agent["status"] != "active":
            fail("agent_inactive", "Agent is not active", 409)
        if environment_id not in agent["environments"] or not owner.allows("environments", environment_id):
            fail("forbidden", "Agent is not authorized in this environment", 403)
        if cap["id"] not in agent["capabilities"] or not owner.allows("capabilities", cap["id"]):
            fail("forbidden", "Capability is not granted to this agent", 403)
        if any(not owner.allows("permissions", p) for p in cap["permissions"]):
            fail("forbidden", "Required capability permission is missing", 403)
        if cap["status"] != "available":
            fail("capability_unavailable", "Capability is unavailable", 409)

    def _validate_data(self, schema, data):
        if len(json.dumps(data, allow_nan=False)) > 32768:
            fail("payload_too_large", "Capability data exceeds 32768 characters", 413)
        if not Draft202012Validator(schema).is_valid(data):
            fail("schema_violation", "Data does not match the capability schema", 422)

    def _submit_action(self, ctx, agent_id, req: c.ActionRequest):
        agent = self._agent(ctx, agent_id)
        key = f"{agent_id}/{req.id}"
        old = self.store.get("action", key)
        if old:
            if any(old[k] != v for k, v in req.model_dump().items()):
                fail("conflict", "Action ID already used with different input", 409)
            return self._refresh(old)
        env = self._env_access(ctx, req.environment_id)
        cap = next((v for v in env["capabilities"] if v["id"] == req.capability and v["version"] == req.version), None)
        if cap is None:
            fail("unsupported_capability", "Capability or exact version is not advertised", 422)
        self._authorize_action(agent, env["id"], cap)
        self._validate_data(cap["input_schema"], req.input)
        self._quota("action", "agent_id", agent_id, self.limits.max_actions_per_agent)
        pending = [self._refresh(a) for a in self.store.all("action") if a["environment_id"] == env["id"] and a["capability"] == cap["id"]]
        if sum(a["state"] not in TERMINAL for a in pending) >= cap["constraints"]["max_concurrent"]:
            fail("quota_exceeded", "Capability concurrency limit reached", 429)
        action = c.Action(**req.model_dump(), agent_id=agent_id, created_at=float(self.clock()),
                          deadline=float(self.clock() + cap["constraints"]["timeout_seconds"]), capability_snapshot=cap).model_dump()
        self.store.put("action", key, action)
        self._event(agent_id, "action.queued", {"action_id": req.id}, env["id"])
        return action

    def _refresh(self, action):
        if action["state"] in TERMINAL:
            return action
        error = None
        if self.clock() >= action["deadline"]:
            error = c.Diagnostic(code="deadline_exceeded", message="Action deadline elapsed")
        else:
            try:
                env = self._get("environment", action["environment_id"])
                adapter = self._principal(env["owner"])
                if not adapter.allows("environments", env["id"]) or not adapter.allows("capabilities", action["capability"]):
                    fail("forbidden", "Adapter grant was withdrawn")
                current = next((v for v in env["capabilities"] if v["id"] == action["capability"] and v["version"] == action["version"]), None)
                if current is None:
                    fail("capability_unavailable", "Capability removed")
                self._authorize_action(self._get("agent", action["agent_id"]), env["id"], current)
                self._authorize_action(self._get("agent", action["agent_id"]), env["id"], action["capability_snapshot"])
            except ProtocolError:
                error = c.Diagnostic(code="authorization_revoked", message="Action is no longer authorized")
        if error:
            action.update(state="failed", error=error.model_dump())
            self.store.put("action", f"{action['agent_id']}/{action['id']}", action)
            self._event(action["agent_id"], "action.failed", {"action_id": action["id"], "error": error.model_dump()}, action["environment_id"])
        return action

    def _action(self, ctx, agent_id, action_id):
        self._agent(ctx, agent_id)
        return self._refresh(self._get("action", f"{agent_id}/{action_id}"))

    def _cancel_action(self, ctx, agent_id, action_id):
        action = self._action(ctx, agent_id, action_id)
        if action["state"] in TERMINAL or action["state"] == "cancel_requested":
            return action
        if not action["capability_snapshot"]["cancellation"]:
            fail("cancellation_unsupported", "Capability does not support cancellation", 409)
        action["state"] = "cancelled" if action["state"] == "queued" else "cancel_requested"
        self.store.put("action", f"{agent_id}/{action_id}", action)
        self._event(agent_id, "action." + action["state"], {"action_id": action_id}, action["environment_id"])
        return action

    def _poll_actions(self, ctx, environment_id):
        self._env_access(ctx, environment_id, execute=True)
        actions = [self._refresh(a) for a in self.store.all("action") if a["environment_id"] == environment_id]
        return [a for a in actions if a["state"] not in TERMINAL]

    def _update_action(self, ctx, environment_id, agent_id, action_id, req: c.ActionUpdate):
        self._env_access(ctx, environment_id, execute=True)
        action = self._get("action", f"{agent_id}/{action_id}")
        if action["environment_id"] != environment_id:
            fail("forbidden", "Action belongs to another environment", 403)
        action = self._refresh(action)
        update = req.model_dump()
        if all(action[k] == v for k, v in update.items()):
            return action  # replay of last successful update
        edges = {"queued": {"acknowledged", "failed"}, "acknowledged": {"running", "completed", "failed"},
                 "running": {"running", "completed", "failed"}, "cancel_requested": {"cancelled", "failed", "completed"}}
        if req.state not in edges.get(action["state"], set()):
            fail("invalid_transition", "Action state transition is not allowed", 409)
        if req.progress < action["progress"]:
            fail("invalid_progress", "Progress must not decrease", 409)
        if req.state == "completed":
            self._validate_data(action["capability_snapshot"]["output_schema"], req.result)
            if req.error:
                fail("invalid_result", "Completed action cannot contain an error", 422)
        elif req.result is not None:
            fail("invalid_result", "Only completed actions may contain results", 422)
        if (req.state == "failed") != (req.error is not None):
            fail("invalid_result", "Only failed actions must contain an error", 422)
        action.update(update)
        self.store.put("action", f"{agent_id}/{action_id}", action)
        self._event(agent_id, "action." + req.state, {"action_id": action_id, "progress": req.progress}, environment_id)
        return action

    def _observe(self, ctx, environment_id, req: c.ObservationRequest):
        self._env_access(ctx, environment_id, execute=True)
        agent = self._get("agent", req.agent_id)
        if environment_id not in agent["environments"]:
            fail("forbidden", "Observation agent is not bound to this environment", 403)
        # Observation deduplication shares bounded event retention; adapters must retain IDs.
        for event in self.store.all("event"):
            if event["agent_id"] == req.agent_id and event["environment_id"] == environment_id and event["type"] == "observation" and event["data"]["id"] == req.id:
                if event["data"] != req.model_dump():
                    fail("conflict", "Observation ID already used", 409)
                return event
        return self._event(req.agent_id, "observation", req.model_dump(), environment_id)

    def _events(self, ctx, agent_id, after=0, limit=100):
        self._agent(ctx, agent_id)
        if after < 0 or not 1 <= limit <= 100:
            fail("invalid_cursor", "Cursor or page size is invalid", 422)
        latest = (self.store.get("sequence", agent_id) or {"value": 0})["value"]
        if after > latest or after < max(0, latest - self.limits.max_events_per_agent):
            fail("cursor_expired", "Cursor outside retained event range; resynchronize resources", 409)
        events = [e for e in self.store.all("event") if e["agent_id"] == agent_id and e["sequence"] > after][:limit]
        cursor = events[-1]["sequence"] if events else after
        agent = self._agent(ctx, agent_id)
        events = [e for e in events if not e["environment_id"] or (
            e["environment_id"] in agent["environments"] and ctx.principal.allows("environments", e["environment_id"]))]
        return c.EventPage(events=events, next_cursor=cursor).model_dump()

    def _context(self, ctx, agent_id):
        self._agent(ctx, agent_id)
        events = [e for e in self.store.all("event") if e["agent_id"] == agent_id
                  and e["type"] in {"observation", "message"}][-100:]
        # Current environment grants apply even to previously received observations.
        agent = self._agent(ctx, agent_id)
        events = [e for e in events if not e["environment_id"] or (
            e["environment_id"] in agent["environments"] and ctx.principal.allows("environments", e["environment_id"]))]
        return {"knowledge": self._knowledge(ctx, agent_id), "events": events}

    def _message(self, ctx, req: c.MessageRequest, sender_id=None):
        if sender_id:
            sender = self._agent(ctx, sender_id)
            sender_id = sender["id"]
        elif ctx.agent_id:
            sender_id = self._agent(ctx, ctx.agent_id)["id"]
        else:
            self._require_role(ctx, "controller", "admin")
        recipient = self._get("agent", req.recipient)
        if recipient["owner"] != ctx.principal.id and not ctx.principal.allows("permissions", "message:" + req.recipient):
            fail("forbidden", "Messaging this agent is not authorized", 403)
        if ctx.agent_id and req.recipient != ctx.agent_id and not ctx.principal.allows("permissions", "message:" + req.recipient):
            fail("forbidden", "Agent messaging requires an explicit recipient grant", 403)
        return self._event(req.recipient, "message", {"sender": sender_id or ctx.principal.id, "text": req.text,
                                                    "sender_type": "agent" if sender_id else "user"})

    def _set_knowledge_sources(self, ctx, agent_id, sources):
        agent = self._agent(ctx, agent_id)
        if any(s not in c.SOURCES or not ctx.principal.allows("knowledge_sources", s) for s in sources):
            fail("forbidden", "Knowledge policy exceeds administrator grants", 403)
        agent["knowledge_sources"] = list(dict.fromkeys(sources))
        self.store.put("agent", agent_id, agent)
        return agent

    def _put_knowledge(self, ctx, agent_id, req: c.KnowledgeRequest):
        agent = self._agent(ctx, agent_id)
        if req.source not in agent["knowledge_sources"] or not ctx.principal.allows("knowledge_sources", req.source):
            fail("forbidden", "Knowledge source is disabled", 403)
        if req.verified and (req.source == "user" or not ctx.principal.allows("permissions", "knowledge.verify")):
            fail("unverified_claim", "User claims cannot be verified; other verification requires permission", 403)
        if req.scope == "environment" or req.source == "environment":
            if not req.environment_id or req.environment_id not in agent["environments"]:
                fail("forbidden", "Knowledge environment is not bound to this agent", 403)
            self._env_access(ctx, req.environment_id)
        if req.scope != "shared" and req.shared_with:
            fail("invalid_scope", "Recipients require shared scope", 422)
        for recipient in req.shared_with:
            self._get("agent", recipient)
            if not ctx.principal.allows("permissions", "knowledge.share:" + recipient):
                fail("forbidden", "Sharing requires an explicit recipient grant", 403)
        self._quota("knowledge", "agent_id", agent_id, self.limits.max_knowledge_per_agent)
        item = c.Knowledge(**req.model_dump(), id=str(uuid.uuid4()), owner=ctx.principal.id,
                           agent_id=agent_id, created_at=float(self.clock())).model_dump()
        self.store.put("knowledge", item["id"], item)
        return item

    def _knowledge(self, ctx, agent_id):
        agent = self._agent(ctx, agent_id)
        result = []
        for item in self.store.all("knowledge"):
            if item["source"] not in agent["knowledge_sources"] or not ctx.principal.allows("knowledge_sources", item["source"]):
                continue
            if item["environment_id"] and (item["environment_id"] not in agent["environments"] or
                                            not ctx.principal.allows("environments", item["environment_id"])):
                continue
            own = item["agent_id"] == agent_id
            shared = item["scope"] == "shared" and agent_id in item["shared_with"] and "shared" in agent["knowledge_sources"] and ctx.principal.allows("knowledge_sources", "shared")
            environmental = item["scope"] == "environment" and item["environment_id"] in agent["environments"] and ctx.principal.allows("environments", item["environment_id"])
            if own or shared or environmental:
                result.append(item)
        return result

    def _audit(self, ctx, after=0, limit=100):
        self._require_role(ctx, "admin")
        if after < 0 or not 1 <= limit <= 100:
            fail("invalid_cursor", "Cursor or page size is invalid", 422)
        rows = self.store.db.execute(
            "SELECT sequence,time,principal_id,operation,resource,outcome FROM audit WHERE sequence>? ORDER BY sequence LIMIT ?",
            (after, limit)).fetchall()
        return [dict(zip(c.AuditEntry.model_fields, row)) for row in rows]

    def _create_task(self, ctx, agent_id, req: c.TaskRequest):
        agent = self._agent(ctx, agent_id)
        if agent["status"] != "active":
            fail("agent_inactive", "Agent is not active", 409)
        if req.model and not ctx.principal.allows("models", req.model):
            fail("forbidden", "Model route is outside administrator grants", 403)
        self._quota("task", "agent_id", agent_id, self.limits.max_tasks_per_agent)
        task = c.Task(id=str(uuid.uuid4()), agent_id=agent_id, goal=req.goal, state="running").model_dump()
        self.store.put("task", task["id"], task)
        return task

    def _finish_task(self, ctx, agent_id, task_id, state, response=None, error=None, actions=None):
        self._agent(ctx, agent_id)
        task = self._get("task", task_id)
        if task["agent_id"] != agent_id or task["state"] != "running":
            fail("conflict", "Task cannot be updated", 409)
        action_ids = []
        for req in actions or []:
            action_ids.append(self._submit_action(ctx, agent_id, req)["id"])
        task.update(state=state, response=response, error=error, action_ids=action_ids)
        task = c.Task.model_validate(task).model_dump()
        self.store.put("task", task_id, task)
        self._event(agent_id, "task." + state, {"task_id": task_id})
        return task

    def _tasks(self, ctx, agent_id):
        self._agent(ctx, agent_id)
        result = []
        for task in self.store.all("task"):
            if task["agent_id"] != agent_id:
                continue
            if task["state"] == "waiting":
                actions = [self._refresh(self._get("action", f"{agent_id}/{key}")) for key in task["action_ids"]]
                if all(a["state"] in TERMINAL for a in actions):
                    task["state"] = "completed" if all(a["state"] == "completed" for a in actions) else "failed"
                    if task["state"] == "failed":
                        task["error"] = c.Diagnostic(code="action_failed", message="At least one task action failed or was cancelled").model_dump()
                    self.store.put("task", task["id"], task)
                    self._event(agent_id, "task." + task["state"], {"task_id": task["id"]})
            result.append(task)
        return result
