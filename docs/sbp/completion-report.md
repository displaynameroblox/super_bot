# SBP v0.1 minimal-core completion report

Implemented incrementally on 2026-11-08. Changes are preserved in the working tree and checkpoint archives; existing chat components were not rewritten. A concurrent change under `src/server/modules/ai_handler` is outside this implementation.

## Delivered

| Deliverable | Location |
| --- | --- |
| Universal host core | `Ai/universal/host.py`, `store.py`, `security.py`, `contracts.py` |
| Optional runtime and provider bridge | `Ai/universal/runtime.py` |
| Separate HTTPS host / CLI | `Ai/universal/http.py`; `super-bot-host` |
| SBP v0.1 specification | [Specification](specification.md) |
| Exact endpoints, methods, authentication, request/response schemas | [API index](api-reference.md), [OpenAPI](openapi.json), [JSON Schema](schemas.json) |
| Adapter interface and capability registry | `Ai/universal/adapter.py` |
| Mock game and robotics adapters | `Ai/universal/mock.py`; `super-bot-mock` |
| Real local HTTPS demonstration | `python -m Ai.universal.demo` |
| Request/response examples | [Validated examples](examples.json) |
| Authentication, configuration, limits, model setup | [Configuration guide](configuration.md) |
| SDK registration, execution, reconnection and safety rules | [Integration guide](adapter-guide.md) |
| Automated acceptance/security tests | `Ai/tests/test_universal.py` |
| Repository assessment and migration | [Migration report](../universal-migration.md) |
| Incremental validation and remaining work | [Checkpoints](checkpoints.md) |
| Regeneration command | `python -m tools.export_sbp` |

## Operational result

One host serves multiple independent environment registrations and agents. Agents are independent of adapters and may bind to multiple explicitly authorized environments. Exact-version capability selection, schema validation, permission checks, idempotent action requests, progress, cancellation, terminal results and structured errors are implemented. Adapters execute through their own native handler registry; arbitrary capability IDs require no core modification.

Sessions, environment/agent identities, actions, event cursors, tasks, knowledge, revocations and audit records persist in a separate SQLite database. Session reconnection rotates credentials while retaining identity. A mock result ledger survives adapter restarts and replays an undelivered result. Event retention and all major object categories are bounded. Knowledge preserves ownership, scope, provenance, confidence and unverified user claims. The runtime uses explicit plans and optional model routes with bounded concurrency and authorized context.

Supported network transport: verified **HTTPS JSON with polling**. Transport-neutral host operations, a typed SDK binding and an in-process embedding path are available. No cloud service or model is required for local action execution. Existing provider routing is opt-in. The chat gateway, existing TLS/encryption interfaces, client-owned data, TUI, model management and coding-agent approval behavior remain separate.

## API authorization summary

All business routes live under `/sbp/v0.1`. All require TLS. `/health`, `/sbp/v0.1/protocol` and `/openapi.json` are public. `POST /sessions` requires a provisioned bootstrap bearer; all other business operations require a valid nonexpired session bearer. See the generated API index for every method and exact body/response type.

Only adapter principals may register environments, poll their action queue, submit native action updates and submit observations; each is limited to owned environments and configured grants. Agents are created by controllers/adapters/admins and accessed by their owner or administrator. Agent-bound credentials cannot mint more credentials or access another agent. Action requests and task plans additionally require environment, capability and capability-specific permission grants. Message sharing and knowledge sharing require explicit recipient grants across the documented boundaries. Audit reads and principal revocation require an unbound administrator session; session revocation is self/admin only.

## Validation

- Final full regression: `python -m pytest Ai/tests -q --tb=short` → **498 passed, 198 subtests passed, 1 deprecation warning** in 55.91 seconds.
- Includes **27 universal-core tests** covering independent environments, concurrent agents, real verified HTTPS, action lifecycle, unsupported actions, schema validation, persistence/reconnect, result replay, isolation, revocation, knowledge permissions, runtime failure/atomicity, local safety and generated reference consistency.
- Existing Super-Bot tests pass with the declared dependencies and local networking available.
- Three implementation checkpoints were preserved; the final archive is `/tmp/super-bot-sbp-checkpoint-3.tar.gz`.

The initial checkout had 29 baseline failures caused mostly by restricted loopback networking and missing/mismatched installed dependencies. The local development environment was aligned with the existing `argon2-cffi` and `websockets>=14,<16` declarations; the new `jsonschema>=4.23,<5` dependency and declared package build tools were installed. One intermediate run encountered a temporary disk-space error; the affected test passed on rerun. No legacy production code was changed to make these tests pass.

The package build succeeded. The wheel includes the entire new subpackage and both new console entry points; imports and generated API access were checked from the wheel outside the source checkout. The generated OpenAPI matches the implementation, and checked-in example requests/responses validate against it. One existing FastAPI/Starlette test-client deprecation warning remains.

## Known limitations and remaining work

This is a functioning minimal core, not the complete long-term distributed framework.

1. **Single process and executor.** One host process per database and one active consumer per environment. Add worker leases, durable scheduling and distributed coordination before scaling workers or mesh deployment.
2. **Transport extensions.** WebSockets, local IPC sockets, network host discovery and mesh networking have interface boundaries but no implementation yet.
3. **Planning and goals.** The runtime accepts explicit plans and optional advisory model text. Autonomous model-generated planning, hierarchical goals, automatic event-driven reasoning, workflow scheduling and richer task cancellation are future increments.
4. **Native side effects.** At-least-once delivery and result replay do not guarantee exactly-once side effects across a native-operation/ledger crash window. External SDKs must implement native idempotency, stop mechanisms and independent hardware safety controls. No real robotics or Roblox SDK is implemented.
5. **Lifecycle/retention administration.** Agent bindings are immutable after registration. There is no object deletion, action history listing, archival, principal unrevocation or knowledge-record update API. Retained-action/task/knowledge quotas eventually require administrator maintenance or a future retention API. Action deadlines and task completion are reconciled on relevant operations, not by a background scheduler.
6. **Knowledge foundations.** Records are scoped and persisted but not semantically indexed. Add retrieval/search, explicit legacy-library import, ownership transfer and knowledge-sharing revocation/update workflows. Provenance and confidence remain attributed claims; no automatic fact verification is provided.
7. **Provider integration.** The existing model-provider callable is reusable through the opt-in bridge. Specialized coding, multimodal, Chatter-tier selection and robotics providers require their own wrappers. Existing coding approval requirements must be retained. In-flight inference is not resumable after host restart.
8. **Operational hardening.** No distributed quotas, at-rest application encryption, tamper-evident audit export, request tracing backend or external secret manager. A hostile adapter can fabricate observations/results in its assigned environment. Deploy with private files, verified certificates and scoped credentials; larger deployments need additional operational controls.
9. **Compatibility/UI.** Existing chat, manager and TUI entry points remain operational but do not expose SBP controls. Add explicit opt-in bridges later. Existing installations must install the new schema-validator dependency before using this core; the update manager's dependency checks still apply.
10. **Release preparation.** This checkout did not contain a top-level license file. A maintainer must establish the intended open-source license and release/version policy before public redistribution; this implementation does not invent licensing authority.

Each next increment can extend these modules without replacing the working protocol and transport core.
