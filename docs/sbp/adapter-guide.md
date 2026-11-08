# External SDK integration guide

A minimal integration requires only verified HTTPS, JSON and a polling loop. Start with [configuration](configuration.md), the [endpoint index](api-reference.md), [complete OpenAPI](openapi.json), [wire schemas](schemas.json) and [examples](examples.json). The authoritative lifecycle and trust rules are in the [SBP specification](specification.md).

## First connection

1. Have the administrator provision one adapter principal with an environment-ID allowlist and capability advertisement allowlist. Keep its random bootstrap token outside source code. Verify the host certificate using an independently trusted public certificate/CA.
2. Send `POST /sbp/v0.1/sessions` with bootstrap bearer and `{"versions":["0.1"]}`. Store the returned session ID, version, expiration and token privately.
3. Send `POST /sbp/v0.1/environments` with the session bearer. Use stable environment IDs and generic capability descriptors. Environment `kind` is descriptive only.
4. Optionally register agents with `POST /agents`; alternatively, let an authorized controller create agents bound to your environment. Your adapter need not own every agent it serves. Only advertise capabilities it can execute safely.
5. Submit `POST /environments/{id}/observations` with a stable observation ID, agent ID, generic kind, data and observation timestamp. The host attributes the environment from the route.

## Native execution loop

Poll `GET /environments/{id}/actions`. Validate responses against the wire contract and verify capability/version locally. Check the action deadline, native resource availability, native permissions and local safety controls. These checks remain necessary even when the host authorized the action.

Before any native side effect, acknowledge with `POST /environments/{id}/agents/{agent}/actions/{action}` and `{"state":"acknowledged"}`. Use `running` and monotonic `progress` for ongoing work. Continue polling for `cancel_requested`. Return `completed` with an output-schema-valid result, or `failed` with a sanitized diagnostic; acknowledge accepted cancellation with `cancelled`. Never report completion before native execution actually completes.

Maintain a durable local ledger keyed by environment/agent/action ID. On repeated delivery, resume or resend the existing result. Only one executor may consume an environment in v0.1. Native non-idempotent operations need their own deduplication/transaction mechanism; host persistence alone cannot guarantee exactly-once side effects. If a deadline expires or authority disappears while disconnected, stop safely using your own controls. There is no host push channel that can instantly interrupt disconnected hardware.

On a transient connection failure, retain state and reconnect with bounded backoff. For a valid session use `POST /sessions/reconnect`, then atomically store the rotated grant. If expired or the rotation response was lost, authenticate again with the bootstrap credential and re-register the same environment. Agents, queues, event cursors, knowledge and tasks retain their identities in host storage. A 401 after administrator revocation requires administrator intervention; do not loop aggressively trying the same credential.

## Python extension points

- `Ai.universal.contracts`: typed engine-neutral request/response contracts.
- `Ai.universal.adapter.Transport`: async request binding for alternate transports.
- `HTTPTransport`: verified TLS, bounded timeout, no credential-bearing redirects.
- `Adapter`: session negotiation/rotation, environment/agent registration, observations, polling and updates.
- `CapabilityRegistry`: register a descriptor plus native handler without core modifications; validates native input and output.
- `MockAdapter`: executable example of progress, independent local safety, durable result replay and the same protocol for game/robotics labels.
- `Host` and `Store`: embeddable local service. Use `Host.invoke` so authentication, transactions, quotas and audit remain enforced.
- `Runtime` / `ModelProvider`: optional model routing and explicit task plans. External model providers are not required for adapter execution.

For asynchronous or long-running native actions, implement a cancellable worker and local resource locks in your SDK. The minimal registry demonstration uses synchronous handlers and does not provide a general hardware scheduler. Do not import engine code into the host, mix another adapter's credentials into your process, or expose raw operating-system command execution as a broadly granted capability.

## Compatibility checklist for SDK authors

Exercise independent environments, at least two agents, unsupported version/capability input, output-schema rejection, repeated action delivery, lost completion response, reconnect/restart, cancellation races, local safety refusal, permission revocation, timeout and event retention expiry. Confirm your SDK never gains access to another agent's private memory. The repository's `Ai/tests/test_universal.py` and `python -m Ai.universal.demo` demonstrate these cases.

Roblox, Minecraft, Unity, Unreal, desktop and ROS 2 SDKs remain separate integrations. No Roblox SDK is implemented or modified here.
