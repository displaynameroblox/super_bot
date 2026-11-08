# Super-Bot Protocol (SBP) v0.1

Status: implemented minimal local-host protocol. SBP connects independently identified agents and environment adapters without depending on an engine, operating system, hardware platform or model. The host owns authorization, orchestration, memory and communication. Adapters own perception, native execution and independent safety enforcement.

Normative wire field definitions are the generated [JSON Schemas](schemas.json). The [OpenAPI document](openapi.json) defines the exact implemented HTTP binding, methods, parameters, request/response bodies and authentication scheme. The readable [endpoint index](api-reference.md) and [examples](examples.json) accompany them. Unknown fields are rejected. JSON numbers must be finite. Timestamps are Unix seconds, UTC; host deadlines use host time.

## Version and transport separation

`GET /sbp/v0.1/protocol` reports supported versions and transports. Session creation supplies a list of supported versions. The host selects `0.1` if present, otherwise returns HTTP 426 `version_mismatch`; it never silently chooses an incompatible version. Capability versions are independent opaque strings and require exact matches.

The service boundary is `Host.invoke(session_token, operation, **arguments)`, accepting the typed contracts without HTTP dependencies. HTTP maps methods/paths onto these operations and maps protocol failures onto HTTP status codes. The SDK's `Transport.request()` interface isolates its request binding. Additional transports must authenticate sessions and dispatch the same validated contracts through the host service, preserving errors and authorization. Internal runtime operations are not automatically public routes.

Implemented network transport: **HTTPS + JSON with polling**. Polling is short polling; clients choose a bounded interval (mock default one second). No push WebSocket, IPC socket, mesh, broker, distributed lock, or UDP SBP host discovery exists yet. In-process embedding is supported. Environment discovery means an authenticated list of authorized registered environments on a known host address.

## Identities and registration

Administrator-provisioned principals have independent bootstrap credentials. Their role and grants come only from private host configuration, not from adapter advertisements. A principal authenticates with a bootstrap bearer to create a session. Session tokens are opaque and stored as hashes. Sessions reference stable principal IDs and optionally one agent ID.

Adapters register environments using a stable ID, display name, generic `kind` label and capability descriptors. `kind` is opaque to the host; the host does not branch on game/robotics labels. Only the owning adapter may re-register or execute for an environment. Re-registration updates advertisements and returns the same environment identity. It cannot take another principal's environment ID.

Controllers, adapters and admins create agents. An agent has a stable caller-selected ID, owner, lifecycle status, explicit environment bindings, allowed capability IDs and allowed knowledge-source categories. It may have no environment. Multiple bindings require explicit grants for every environment. Repeating identical agent registration is idempotent; changing an existing configuration returns 409. Agent-bound sessions authenticate individual agents and cannot create or access other agents. Current lifecycle states are `active`, `paused`, and `stopped`; these are reversible statuses, not deletion. Paused/stopped agents cannot request or continue authorized actions or start tasks.

An adapter may submit observations and execute actions for agents belonging to another controller when those agents explicitly bind to its environment. This does **not** grant the adapter access to those agents' memory, task history or message inbox.

## Capabilities and permission negotiation

Each capability advertises:

- `id`, `version`;
- `input_schema`, `output_schema`;
- required permission names;
- constraints: `timeout_seconds` (1–3600) and `max_concurrent` (1–128);
- availability: `available` or `unavailable`;
- `cancellation`: boolean.

The advertised registry is stored per environment. Developers may use arbitrary valid capability IDs and SDK-local handlers without changing host code. No capability is universally mandatory. Registry registration alone grants no authority: the adapter's advertisement scope, agent's configured capability list, owner's current environment/capability/permission grants and the current advertisement must all permit execution. Action admission and adapter delivery/update recheck these conditions. Required permissions are also rechecked against the action's original snapshot, so changing an advertisement cannot weaken an already queued action's requirements.

The host accepts a bounded subset of JSON Schema 2020-12: `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`, `description`, `title`, `default`, `$schema`. Nested boolean schemas are supported. Remote/local `$ref`, regular expressions, combinators and other keywords are rejected; no schema fetches the network. See configuration limits. Use a new capability version when changing native input/output contracts. A queued action snapshots the accepted contract; its result is validated against that snapshot.

## Action lifecycle and delivery

An action request contains `id`, `environment_id`, `capability`, exact `version`, and `input`. The endpoint identifies the requesting agent. The request ID is an idempotency key **within that agent**, retained with the action. A repeat with identical request fields returns the same action; changed input under that ID returns 409. Unsupported capabilities/versions or invalid input return 422 before queuing. Unauthorized work returns 403.

The host adds agent identity, creation/deadline timestamps, the capability snapshot, state, progress, result and error. The adapter polls its environment for all outstanding actions. Polling does not claim an action or advance its state. Only its owning adapter may update an action, and the route environment must match the stored environment.

| Current state | Accepted next states |
| --- | --- |
| `queued` | `acknowledged`, `failed`; owner cancellation → `cancelled` |
| `acknowledged` | `running`, `completed`, `failed`; owner cancellation → `cancel_requested` |
| `running` | `running`, `completed`, `failed`; owner cancellation → `cancel_requested` |
| `cancel_requested` | `cancelled`, `failed`, `completed` |
| `completed`, `failed`, `cancelled` | Terminal; exact replay of the last update only |

Progress is a finite number from 0 to 1 and cannot decrease. A completed action must match the output schema and cannot carry an error. A failed action must carry a structured error. Other updates cannot carry a result or error. Set progress explicitly when reporting completion/cancellation; the default is 0. Exact update replay is idempotent; replaying an older state after a newer one returns 409.

Cancellation requires advertised support. Cancellation of queued work completes locally; cancellation of accepted work is a request that must reach the adapter. A concurrent completion may win the race and is accepted. Cancellation never promises rollback of physical/native effects.

Deadlines and withdrawn authority turn outstanding actions into `failed` on the next relevant poll/read/update. The host has no background action scheduler in this increment. Adapters must enforce deadlines locally even while disconnected, retain their execution ledger and honor independent local stop controls. One active consumer per environment/adapter principal is required. There are no worker leases or exactly-once native side effects. The mock saves completed results before delivery and replays them after reconnection; a crash between a native side effect and ledger persistence remains an adapter responsibility. Do not blindly retry an uncertain non-idempotent native effect.

## Observations, events and messages

Observations contain `id`, `agent_id`, `kind`, structured `data` and `observed_at`. The route identifies the environment. Only its owning adapter may submit an observation for an agent bound there. Observations are partial, untrusted evidence; they do not imply complete world knowledge. Observation IDs are deduplicated per environment/agent only while their events remain retained.

Events contain per-agent monotonically increasing `sequence`, `type`, `agent_id`, optional `environment_id`, host `time` and typed JSON `data`. Types currently include `observation`, `message`, `agent.state`, `action.<state>`, and `task.<state>`. Domain events can use an observation `kind`; no engine-specific schema is required.

Inbox reads supply `after` and `limit` and return `events` plus `next_cursor`. Save the cursor only after processing the page. Gaps are valid because current environment permissions filter previously received events. A cursor outside the retained range returns 409 `cursor_expired`; resynchronize agents, tasks and known actions and request fresh observations. Cursors and events persist across restarts. The current resynchronization API has no historical action-list endpoint; clients must retain submitted action IDs.

Users/controllers send messages with `recipient` and `text`; the server stamps sender identity. Agent messages identify an authorized sending agent, or use an agent-bound session. Owners may message their own agents; crossing principal boundaries requires `message:RECIPIENT_ID`. Agent-bound credentials require explicit recipient grants even for a different agent of the same owner. Message events retain sender type (`agent`/`user`). Untrusted content is never executed as host instructions.

## Knowledge and minimal runtime

Sources are separate categories: `environment`, `experience`, `developer`, `user`, `shared`, `memory`. Each record preserves content, declared provenance, confidence (0–1), verification status, owner, authoring agent, timestamp and scope. Provenance/confidence are attributed metadata, not an automatic assertion of truth. User claims always remain unverified; attempts to mark a `user` record verified are rejected. Other verification requires administrator-granted `knowledge.verify`.

Default scope is private to the authoring agent, even when two agents share one owner. `shared` scope names explicit recipient agent IDs and requires `knowledge.share:RECIPIENT_ID` for every recipient. `environment` scope is visible only to agents explicitly bound and authorized there. A receiver must enable the source category; receiving shared records additionally requires the `shared` category. Developer/end-user source selection can narrow administrator grants, never widen them. Environment-tagged evidence is filtered when environment access is withdrawn. No legacy client library is imported automatically.

`Runtime` routes bounded authorized knowledge, recent observations/messages and a goal to an explicitly selected injected provider. It accepts an explicit list of up to 16 actions, validates/adopts the entire plan in one transaction, and maintains persisted task state. Model output is advisory text and is **not automatically converted into executable actions**. With no selected model, explicit plans run entirely offline. Different agents can execute concurrently; only one active runtime call per agent is allowed. Resource slots and persistent task quotas bound execution.

A task is `running` during inference, `waiting` after action dispatch, and becomes `completed`/`failed` when polled after its actions terminate. A no-action task completes when its runtime invocation returns; this is not a claim that an arbitrary real-world goal was achieved. In-flight inference is marked failed on host restart, not silently replayed. Authorization is checked again before dispatch after model inference. Inference exceptions are sanitized. The existing synchronous provider's own timeout controls completion; it cannot be forcibly interrupted. Autonomous planning, goal hierarchies, knowledge search/embeddings, scheduling and event-triggered autonomous loops are future work.

## Errors, audit and security boundary

All API errors use `{"error":{"code":"...","message":"...","retryable":false}}`. Status meanings: 400 malformed operation/transport; 401 invalid/expired/revoked credential; 403 scope violation; 404 missing resource; 409 state/identity/cursor conflict; 413 size limit; 422 schema/capability/model mismatch; 426 protocol version mismatch; 429 quota; 500 host failure; 502 provider failure. Clients must use error codes, not parse human messages. Core rate/concurrency errors may set `retryable: true`; do not retry permanent authorization/schema failures unchanged.

The HTTP boundary rejects oversized/deep/nonfinite JSON, requires HTTPS and bearer credentials, applies ingress and principal rate limits, emits no-store responses and does not expose interactive API docs. Protected resource operations are audited with no content or secrets. Environment/account/agent scopes are independent. SBP uses TLS transport protection and **does not inherit** the chat service's additional sealed-envelope protocol. Deploy one private host, trust its certificate, protect local disk/backups and scope credentials narrowly.

An adapter is trusted to report its own environment faithfully. Compromised adapters can fabricate observations/results for their assigned environment; SBP does not attest hardware or verify the physical truth of reports. Physical safety must be enforced independently at the device/controller layer. This host contains no API to disable hardware interlocks, and the mock safety gate is only a demonstration.
