# SDK step-up guide — which API to call and when

This guide targets the **implemented SBP v0.1 host**. Use it to build an environment SDK in any language. The SDK supplies observations and executes native actions; Super-Bot owns agents, permissions, memory and orchestration.

For your first integration, follow steps 1–8 with **one environment, one adapter-owned agent and one capability**. Add cancellation and reconnect handling before adding more capabilities.

## Connection details

| Setting | Value |
| --- | --- |
| Local host origin | `https://127.0.0.1:8767` |
| API base | `https://127.0.0.1:8767/sbp/v0.1` |
| Transport | HTTPS JSON with short polling |
| Request body header | `Content-Type: application/json` |
| Authentication | `Authorization: Bearer TOKEN` |
| Successful business response | HTTP `200`, with a JSON body |

**All paths below include the API prefix.** Replace the host origin when connecting from another computer or a hosted environment: `127.0.0.1` always means the machine making the request. Your environment must be able to reach the host and validate its TLS certificate. The existing chat server on port 8000 uses a different API.

Before starting, follow [host setup](docs/sbp/configuration.md). Obtain the host address, trusted certificate/CA and an **adapter bootstrap token** from the administrator. For the examples, the adapter principal must have:

- Role: `adapter`.
- Environment grant: `game`.
- Capability grant: `navigation.move_to`.
- Permission: `move` (needed when this principal also owns the agent requesting movement).
- Knowledge-source grants for the categories selected at registration below.

Keep credentials in the trusted adapter/server process. A UI or untrusted game client should not receive the adapter bootstrap/session token.

## Call order at a glance

| When | Call | Credential |
| --- | --- | --- |
| Optional reachability check | `GET /health` | None |
| Check protocol compatibility | `GET /sbp/v0.1/protocol` | None |
| First connection or expired session | `POST /sbp/v0.1/sessions` | Bootstrap token |
| After authenticating | `POST /sbp/v0.1/environments` | Adapter session |
| First agent setup; safe to repeat identically | `POST /sbp/v0.1/agents` | Agent owner's unbound session |
| New relevant perception | `POST /sbp/v0.1/environments/game/observations` | Adapter session |
| Agent/controller chooses an action | `POST /sbp/v0.1/agents/explorer/actions` | Agent owner or agent-bound session |
| Regularly, including while actions run | `GET /sbp/v0.1/environments/game/actions` | Adapter session |
| Accept, progress, finish or fail an action | `POST /sbp/v0.1/environments/game/agents/explorer/actions/move-001` | Adapter session |
| Owner checks an action result | `GET /sbp/v0.1/agents/explorer/actions/move-001` | Agent owner or agent-bound session |
| Owner reads observations/messages/lifecycle events | `GET /sbp/v0.1/agents/explorer/events?after=0&limit=100` | Agent owner or agent-bound session |
| Renew a valid session / resume after interruption | `POST /sbp/v0.1/sessions/reconnect` | Current session |
| End a session deliberately | `DELETE /sbp/v0.1/sessions/SESSION_ID` | That session, or administrator |

An **unbound session** is the ordinary session returned by `/sessions`. An **agent-bound session** is the optional credential created in step 4. Use the unbound adapter session for environment registration, observations, action polling and execution reports.

## 1. Check the host

Call `GET /health` if you want a reachability check; it returns `{"status":"ok"}`.

Call `GET /sbp/v0.1/protocol` before choosing your protocol version. The current response is:

```json
{
  "versions": ["0.1"],
  "transports": ["https-json-polling"]
}
```

Proceed only if your SDK supports an advertised version. There is no implemented WebSocket or SBP UDP discovery path; configure a known host address.

## 2. Authenticate and save the session

Call **`POST /sbp/v0.1/sessions`** using the bootstrap token, with:

```json
{"versions": ["0.1"]}
```

Save these response fields privately:

| Field | Use |
| --- | --- |
| `access_token` | Bearer token for subsequent operations |
| `session_id` | Session identity; also needed for explicit logout |
| `expires_at` | Expiration as Unix seconds; schedule renewal before it |
| `version` | Negotiated version, currently `0.1` |
| `principal_id` | Authenticated adapter/controller identity |
| `agent_id` | `null` for this ordinary session |

Do not keep sending the bootstrap token to business endpoints. Keep one shared session manager per credential and serialize token renewal so concurrent requests do not race a rotation.

## 3. Register the environment and its capabilities

Call **`POST /sbp/v0.1/environments`** using the adapter session:

```json
{
  "id": "game",
  "name": "SDK development environment",
  "kind": "game",
  "capabilities": [
    {
      "id": "navigation.move_to",
      "version": "1",
      "input_schema": {
        "type": "object",
        "properties": {"x": {"type": "number"}},
        "required": ["x"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {"arrived": {"type": "boolean"}},
        "required": ["arrived"],
        "additionalProperties": false
      },
      "permissions": ["move"],
      "constraints": {"timeout_seconds": 60, "max_concurrent": 8},
      "status": "available",
      "cancellation": true
    }
  ]
}
```

The response echoes the registration and adds `owner`. Keep the environment ID stable across reconnects for the same logical environment. Different independent environments need different IDs and administrator grants. `kind` is descriptive; it does not select engine-specific host behavior.

Your SDK must implement a handler for each advertised capability/version. Translate generic inputs into native API calls inside the SDK. The coordinate `x` above is a deliberately small sample contract; define units, coordinates and richer schemas for your real SDK, and use a new version when changing that contract.

Re-register with the **complete** capability list when availability changes; registration replaces the advertisement. Set `cancellation` to `true` only when you implement its handshake. Only a capability's owning adapter can execute its environment's actions.

## 4. Register an agent and bind its native representation

For this first integration, use the same adapter session to call **`POST /sbp/v0.1/agents`**:

```json
{
  "id": "explorer",
  "name": "Explorer",
  "environments": ["game"],
  "capabilities": ["navigation.move_to"],
  "knowledge_sources": ["environment", "experience", "developer", "user", "shared", "memory"]
}
```

The response adds `owner` and `status` (initially `active`). Select only knowledge categories allowed by your administrator; a narrower administrator policy requires a narrower request.

Keep a local mapping such as `explorer → native entity/controller`. Host registration **does not spawn a native object**. Your SDK creates or locates that representation and binds it to the stable agent ID. A native representation disappearing does not automatically delete or stop the host agent.

Repeated identical registration returns the existing agent. Changing configuration under the same ID returns `409 conflict`; environment bindings and capability lists currently have no update endpoint.

For a separate agent process, the owner can optionally call **`POST /sbp/v0.1/agents/explorer/sessions`**, with no body. Give that process the returned agent-bound token. It can access `explorer`'s authorized operations, but cannot perform the adapter's environment execution duties.

**Later, with separate ownership:** a controller authenticates with its own bootstrap token and registers its own agent bound to `game`. The adapter can observe/execute for that agent but cannot read its private knowledge, inbox or tasks. `GET /sbp/v0.1/agents` is an owned-agent list, not a list of every agent bound to your environment; provision native mappings explicitly and use action/observation agent IDs.

## 5. Send observations when relevant state changes

Call **`POST /sbp/v0.1/environments/game/observations`**:

```json
{
  "id": "observation-001",
  "agent_id": "explorer",
  "kind": "position",
  "data": {"x": 0},
  "observed_at": 1700000000.0
}
```

Use the current Unix timestamp for `observed_at`; the value above is illustrative. `kind` and `data` describe partial perception. Send meaningful changes or bounded periodic samples, not an unbounded stream every rendering frame.

The response is an event with `sequence`, `type: "observation"`, `agent_id`, `environment_id`, host `time` and your observation in `data`. Use a new observation ID for a new sample. For delivery retries, reuse the same ID **and the exact same body, including timestamp**. Deduplication only lasts while the observation event remains retained.

Sending an observation does **not** automatically invoke a model or enqueue an action in this minimal runtime.

## 6. Request an action from the agent side

For your initial manual test, the adapter owns `explorer`, so its session can call **`POST /sbp/v0.1/agents/explorer/actions`**:

```json
{
  "id": "move-001",
  "environment_id": "game",
  "capability": "navigation.move_to",
  "version": "1",
  "input": {"x": 4}
}
```

In a deployed integration, this call normally comes from the agent/controller. Your environment execution loop handles the resulting queue.

The response is an `Action` object, initially `state: "queued"`, with `agent_id`, `created_at`, `deadline`, `progress`, `result`, `error` and `capability_snapshot` in addition to the request fields. Exact schemas and full example responses are linked at the end.

An action ID is unique **within its agent**. Store it before submitting. If the response is lost, retry the same ID and body; do not generate a new ID for the retry. A different body under the same ID returns `409 conflict`. Unsupported capability/version returns `422`; missing grants return `403`.

## 7. Poll, acknowledge and execute

Call **`GET /sbp/v0.1/environments/game/actions`** regularly using the adapter session. Start with about once per second and respect your configured request budget. It returns an array of outstanding actions; `[]` means no outstanding work. Polling does not claim or acknowledge an action.

Keep this polling loop responsive while native actions run. Dispatch native work to your SDK's workers and enforce per-resource locks locally. Run only **one active executor per environment** in v0.1.

For each action, check its environment, agent mapping, capability/version, input, deadline and local safety conditions. Look up a durable execution ledger keyed by `(environment_id, agent_id, action_id)` before starting native work.

All updates for the example action go to:

**`POST /sbp/v0.1/environments/game/agents/explorer/actions/move-001`**

| When | JSON request body |
| --- | --- |
| Accept queued work, before native effects | `{"state":"acknowledged","progress":0.0}` |
| Execution is in progress | `{"state":"running","progress":0.5}` |
| Native execution actually succeeded | `{"state":"completed","progress":1.0,"result":{"arrived":true}}` |
| Execution failed at progress 0.5 | `{"state":"failed","progress":0.5,"error":{"code":"native_execution_failed","message":"Movement could not finish","retryable":false}}` |

Wait for a successful acknowledgment before starting native effects. A short action may go directly from `acknowledged` to `completed`; `queued → completed` is rejected. If you reject queued work locally, report `failed` with progress `0.0` and an error.

Every update returns the full current `Action`. Progress must never decrease. Always send its current value on failure/cancellation: omitted progress defaults to `0.0`. Only completion carries a result; its result must match the advertised output schema. Only failure carries an error, and failure requires one.

Persist the final result locally before submitting it. On repeated delivery, resume existing work or resend the saved result. Exact replay of the last accepted update is allowed; replay of an older state after a newer one returns `409 invalid_transition`.

If an action disappears from the outstanding queue, do not start/restart it. It may have finished, expired or lost authorization. Reconcile your worker safely; an authorized owner can read its final state using the action GET route. A native side effect and the ledger write are not one host transaction: your SDK needs native idempotency or reconciliation for crashes between them.

## 8. Read results and events from the agent side

The owner or agent-bound session calls **`GET /sbp/v0.1/agents/explorer/actions/move-001`** to read that action's state and result. Stop waiting when the state is `completed`, `failed` or `cancelled`.

For observations, messages and lifecycle notifications, call:

**`GET /sbp/v0.1/agents/explorer/events?after=0&limit=100`**

The response is `{"events":[...],"next_cursor":N}`. Process the page, then persist `next_cursor` and use it as `after` on the next call. Maintain one cursor per agent. Event data varies by type; action lifecycle events are notifications, so fetch the action itself for its full result.

Cursors can have gaps due to permission filtering. `409 cursor_expired` means retained history no longer covers the cursor: resynchronize known agents/actions/tasks and request fresh observations. The current API has no action-history listing endpoint, so retain your submitted action IDs. Events are private to their agent owner; a separate adapter does not read a controller-owned inbox.

## 9. Handle cancellation

When the owner wants to cancel, it calls **`POST /sbp/v0.1/agents/explorer/actions/move-001/cancel`**, with no body.

| Host response state | SDK behavior |
| --- | --- |
| `cancelled` | Work was still queued; do not execute it |
| `cancel_requested` | Adapter sees this on polling; request native stop and keep tracking the worker |
| A terminal state | No further execution; the action already finished |

After native cancellation is confirmed, the adapter reports to the same action-update endpoint:

```json
{"state": "cancelled", "progress": 0.5}
```

Replace `0.5` with the action's latest progress. The adapter cannot arbitrarily send `cancelled` from `running`; the host must first be in `cancel_requested`. A local safety refusal can be reported as `failed`.

If work genuinely completed while cancellation raced with it, `completed` is accepted from `cancel_requested`. Cancellation is not a guarantee of undoing side effects. Unsupported cancellation returns `409 cancellation_unsupported`. Enforce deadlines and local stop controls even without a connection.

## 10. Reconnect without losing identity

For a valid session, call **`POST /sbp/v0.1/sessions/reconnect`**, with no body and the current session bearer. It returns a new grant with the **same session ID** and a **new access token/expiration**. Atomically replace the saved token; the old token immediately becomes invalid.

If the token expired, or the rotation response was lost, authenticate again using **`POST /sbp/v0.1/sessions`** and your bootstrap token. This gives a new session ID; environment/agent/action identities remain unchanged.

After reconnecting or restarting your SDK:

1. Re-register the same environment ID and complete capability list.
2. If you own/create the agents, repeat their original registration or read them with `GET /sbp/v0.1/agents/explorer`. Preserve configuration and native mappings.
3. Resume action polling and compare outstanding actions with the durable local ledger.
4. Resume existing native work safely, or resend saved results. Do not blindly execute previously acknowledged/running work again.
5. Resume event reads from saved cursors if your process is also an authorized agent client.

Use bounded backoff with jitter for network outages. A `401` from session creation itself may mean the bootstrap credential is revoked or invalid; stop retrying aggressively and surface the configuration problem. Session renewal is not a heartbeat API, and it does not cancel or reissue actions.

## 11. Stop cleanly

For a temporary network outage, preserve state and use the reconnect procedure. For intentional shutdown, coordinate cancellation or completion of running actions, stop native workers safely, and persist their outcomes.

If you own an agent and want to prevent new execution, call **`PATCH /sbp/v0.1/agents/explorer`** with `{"status":"paused"}` or `{"status":"stopped"}`. Use `{"status":"active"}` to resume. Withdrawing authority fails outstanding actions when reconciled; it does not perform a physical stop for you.

Finally, call **`DELETE /sbp/v0.1/sessions/SESSION_ID`** using that session. This revokes the credential but does **not** delete the environment/agent or automatically cancel accepted actions. There is currently no unregister/delete-environment route.

## Optional calls after the execution loop works

These are agent/controller features. Use an authorized agent-owner or agent-bound session; environment ownership alone does not grant them.

| Need | Call | Body / behavior |
| --- | --- | --- |
| Discover authorized environments and capabilities | `GET /sbp/v0.1/environments` | No body; inspect advertisements before choosing actions |
| List owned agents | `GET /sbp/v0.1/agents` | No body |
| Send an agent message | `POST /sbp/v0.1/agents/explorer/messages` | `{"recipient":"other-agent","text":"Hello"}`; recipient grants may be required |
| Send a user/controller message | `POST /sbp/v0.1/messages` | Same body; use a controller session for a user message |
| Store agent memory | `POST /sbp/v0.1/agents/explorer/knowledge` | `{"source":"memory","content":"A route was observed","provenance":"observation-001","confidence":0.5,"verified":false,"scope":"private"}` |
| Read permitted knowledge | `GET /sbp/v0.1/agents/explorer/knowledge` | No body |
| Select enabled knowledge categories | `PUT /sbp/v0.1/agents/explorer/knowledge-sources` | JSON array, e.g. `["experience","memory"]`; cannot exceed administrator grants |
| Submit a goal with an explicit plan | `POST /sbp/v0.1/agents/explorer/tasks` | Example below; returns persisted task state |
| Check task completion | `GET /sbp/v0.1/agents/explorer/tasks` | Reconciles waiting tasks against their actions |

Example explicit task plan:

```json
{
  "goal": "Move to the target",
  "actions": [
    {
      "id": "move-002",
      "environment_id": "game",
      "capability": "navigation.move_to",
      "version": "1",
      "input": {"x": 8}
    }
  ]
}
```

Use this **instead of separately posting the same plan's actions**. A task without a model can dispatch an explicit plan offline. Model selection is optional and requires a configured, granted route such as `"model":"existing"`. Model text is advisory: a goal or observation alone does not produce an autonomous action plan. Task creation has no request idempotency key; after a lost response, inspect task state before creating another task.

Knowledge writes and message sends also lack client idempotency keys; retries may create duplicates. Treat user-supplied claims as `source: "user"` and unverified. Implement sharing only after reading its permission rules.

## Errors your SDK should handle

All structured errors have `error.code`, `error.message` and `error.retryable`. Branch on `code`, not the human-readable message. A timeout with no response does not prove that the host rejected the operation.

| Status / code | What to do |
| --- | --- |
| `401 unauthorized` | Renew/re-authenticate as above; surface revoked/invalid bootstrap credentials |
| `403 forbidden` | Fix ownership/grants/bindings; do not retry unchanged |
| `404 not_found` | Check the resource ID and registration order |
| `409 conflict` | Reused ID with different configuration/input; recover the original request |
| `409 invalid_transition` / `invalid_progress` | Reconcile current state; do not restart the native action |
| `409 agent_inactive` / `capability_unavailable` | Wait for authorized lifecycle/availability changes |
| `409 cursor_expired` | Resynchronize retained state and observations |
| `413 payload_too_large` | Reduce observations/payload size |
| `422 invalid_request` / `schema_violation` | Correct the JSON shape; unknown fields are rejected |
| `422 unsupported_capability` | Select an advertised capability and exact version |
| `426 version_mismatch` | SDK/host protocol versions do not overlap |
| `429 rate_limited` | Back off until the next fixed-minute budget; reduce polling/update traffic |
| `429 quota_exceeded` | Determine whether concurrency or retained storage is full; storage quotas need maintenance, not endless retries |
| `500` / `502` | Surface the failure and reconcile before retrying non-idempotent operations |

The default budget is 600 authenticated operations per minute **per principal**, shared by its sessions. A single poll per second uses about 60 of those calls; observations, progress updates and multiple environments use the same budget. Default request limit is 65,536 bytes. Throttle progress and perception accordingly.

## Your first SDK checkpoint

Build and validate this sequence before adding model reasoning or extra native actions:

```text
authenticate → register environment → register agent
             → send observation → manually request one action
             → poll → acknowledge → execute → report completion
             → owner reads action result and observation event
```

Then verify reconnection with the same identities, duplicate action delivery, lost completion responses, cancellation while running, local safety refusal, unsupported actions and two independent agents/environments. Keep each working stage before extending the SDK.

Reference/demo commands from this repository, with its Python environment active:

```sh
python -m Ai.universal.demo
python -m pytest Ai/tests/test_universal.py -q
```

The demo uses a real local HTTPS listener and no cloud service. The test suite includes loopback networking, so the environment running it must permit local sockets.

## Exact contracts and implementation references

- [All HTTP endpoints and response types](docs/sbp/api-reference.md)
- [OpenAPI specification](docs/sbp/openapi.json) and [wire JSON Schemas](docs/sbp/schemas.json)
- [Full example requests and responses](docs/sbp/examples.json)
- [Protocol lifecycle, capabilities and permission rules](docs/sbp/specification.md)
- [Host configuration and credential provisioning](docs/sbp/configuration.md)
- [Python SDK interface](Ai/universal/adapter.py) and [mock execution loop](Ai/universal/mock.py)
- [Remaining core limitations](docs/sbp/completion-report.md)

SBP v0.1 currently uses one host process and one executor per environment. Additional transports, automatic planning and SDK-specific native behavior remain separate increments. Keep engine-specific objects and native API details inside your SDK; advertise generic capabilities and observations to Super-Bot.
