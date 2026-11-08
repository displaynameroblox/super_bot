# Universal core: repository assessment and migration plan

Assessment completed before implementation, 2026-11-08.

## Existing components and reuse

| Component | Existing behavior | Universal-core decision |
| --- | --- | --- |
| `Ai/server.py`, `gateway.py`, `e2ee.py`, `transport.py` | FastAPI chat API behind an encrypted gateway; temporary server sessions | Preserve all endpoints and encryption behavior. Add a separate opt-in SBP host, not routes to the gateway allowlist. Reuse FastAPI/httpx. |
| `Ai/phone_model.py` | `chat(messages, response_schema, model)` routes to configured local or external providers | Wrap this callable behind an injectable model registry. No provider required to start SBP or run mock adapters. |
| `Ai/session_store.py`, `database.py`, `accounts.py` | Chat/account-specific schemas; active server data is RAM-only | Separate durable SBP SQLite store. Never import client accounts, passwords, chat history, or knowledge automatically. |
| `Ai/knowledge.py`, `client_data.py` | Client-owned SQLite/FTS knowledge with provenance | Preserve storage and APIs. SBP adds explicitly scoped knowledge records; future opt-in importer can reuse passage splitting/search. |
| `Ai/coding_tools.py` | Coding CLI processes, isolated workspace copies, reviewed patches and command approval | Preserve approval flows. Future capability wrappers must retain these checks; do not auto-execute coding commands through SBP. |
| `Ai/chat_app.py`, `tui.py`, `account_app.py`, `web_client.py`, `web_app.py` | Working chat clients and local web interface | No UI rewrite or new SBP controls in this increment. |
| `Ai/tls.py` | Private certificate generation and configurable TLS | Reuse for HTTPS on a separate default port. |
| `Ai/local_manager.py`, `manager_runtime.py`, `managed_model.py` | Server administration and model installation/launch | Keep existing commands unchanged; add a standalone universal-host command. Manager integration is future work. |
| `src/` | Existing engine-specific Luau project | Leave intact. No Roblox SDK implementation in this change. |

## Conflicts

1. Chat's disposable identities and client-owned persistence cannot become durable adapter identities without changing its privacy contract.
2. The existing encrypted gateway intentionally rejects arbitrary REST endpoints. SBP uses its own HTTPS JSON API and credentials; its TLS transport does not claim the chat API's additional sealed-message encryption.
3. Legacy chat schemas contain persona/player concepts and do not represent capability negotiations, action state machines, or environment scope.
4. Existing coding agents are CLI integrations, not independently identified universal agents. Keep both interfaces instead of renaming or replacing them.
5. Current setuptools configuration explicitly packages only `Ai`; include the new subpackage explicitly without changing old entry points.

## Incremental migration

1. Add independent typed protocol contracts, capability validation, durable identity/session/action/event/knowledge storage and scoped authorization.
2. Add a universal runtime with task state, injectable inference routing, bounded concurrency, observation processing and action orchestration.
3. Expose HTTPS JSON/polling, a transport-neutral adapter interface, and two mock adapters using identical contracts.
4. Validate concurrency, reconnect/restart, isolation, permission revocation and lifecycle errors; rerun the existing suite.
5. Publish generated OpenAPI, protocol/schema reference, examples and SDK integration instructions.
6. Later, add explicitly configured UI/manager bridges, real environment SDKs, additional transports, retention administration and distributed coordination. None requires replacing the existing chat stack.

## Compatibility contract

Existing commands, modules, client data and network endpoints retain their behavior. The new host persists only data explicitly submitted to SBP, in a separate administrator-selected directory. Its installation adds a JSON Schema validator dependency and two console entry points. No cloud inference, model download, legacy-data migration or external SDK is required for local protocol operation.
