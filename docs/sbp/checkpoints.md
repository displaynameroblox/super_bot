# Implementation checkpoints

Changes are preserved in the working tree. No existing chat component is rewritten.

## Baseline and design

- Repository assessment and migration plan: `docs/universal-migration.md`.
- Baseline: 442 passed, 29 failed, 196 subtests passed. Most failures require local listening sockets blocked by the initial execution permissions; release tests also found a missing existing `argon2-cffi` dependency.
- Installed the declared existing password dependency and the new JSON Schema validator in the development virtual environment.

## Checkpoint 1 — durable protocol core

- Preserved: typed SBP contracts, bounded JSON Schema profile, transactional SQLite storage, administrator grants, session rotation/expiration/revocation, environment/agent registration, action lifecycle, event cursors, scoped knowledge and audit records.
- Validation: `python -m pytest Ai/tests/test_universal.py -q` → **12 passed**.
- Remaining: HTTP API and generated OpenAPI, TLS launch configuration, adapter transport/interface, mock game/robotics demonstration, runtime model routing, broader acceptance tests, SDK and configuration documentation.
- Deliberate scope: single host process; no mesh, WebSocket implementation, real robotics or engine SDK.

## Checkpoint 2 — HTTPS binding, SDK and mock adapters

- Preserved: separate TLS host command, typed HTTP routes, streaming request bounds, bearer authentication, adapter transport interface, extensible SDK capability handlers, durable mock result ledger, game and robotics mocks using the same protocol.
- Also integrated the minimal runtime needed by the task endpoint: independent agents, explicit plans, optional injectable model routes, bounded concurrent inference and authorized evidence context.
- Validation: `python -m pytest Ai/tests/test_universal.py -q` → **20 passed**. Includes both mock adapters over the HTTP application, independent local safety stop, HTTP isolation and concurrent runtime tasks.
- Remaining: real socket/TLS smoke test, lost-response replay regression, generated schema/reference artifacts, configuration and external SDK guides, final legacy regression run and packaging check.
- Deferred intentionally: autonomous model-generated planning, distributed workers, real hardware integration and UI controls.

## Checkpoint 3 — acceptance, references and packaging

- Preserved: real verified-HTTPS demo; lost-result replay across adapter restart; persisted pending actions, memory and revocation tests; runtime authorization recheck; current permission filtering of retained evidence; generated OpenAPI and JSON Schema; validated wire examples; configuration, SDK, API and completion guides.
- Focused validation before final regression: **26 passed, 1 real-network test deselected**; the real HTTPS test passed in the network-enabled regression run.
- Affected legacy/new regression after correcting existing dependency mismatch: **79 passed, 39 subtests passed**.
- Packaging: wheel built successfully; new modules and both console entry points verified outside the checkout. Existing public commands preserved.
- Remaining: see the bounded follow-up list in `completion-report.md`. No real SDK, autonomous planner or distributed transport was added.
- Final full-suite result: **498 passed, 198 subtests passed, 1 existing deprecation warning** in 55.91 seconds.
