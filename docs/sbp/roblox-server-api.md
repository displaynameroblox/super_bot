# Roblox server API client

`src/server/modules/server_api.luau` exports the SBP request/response types and all 28 HTTP endpoint methods. Existing `start_up()` and `issue_cmd()` continue to handle Roblox remote commands.

Configure once on the server, using the HTTPS origin of the SBP service:

```luau
local api = require(script.Parent.server_api)
api.configure("https://your-sbp-host:8767")

local ok, grant = api:create_session(bootstrapToken, { versions = { "0.1" } })
if not ok then
    warn(grant.code, grant.message)
    return
end
local token = grant.access_token
local listed, environments = api:list_environments(token)
```

Use dot syntax for `configure`; use colon syntax for endpoint methods. Every protected method takes its bearer token first, then path IDs in route order, then its request body (if any). `create_session` takes a bootstrap token; the other protected methods take a session token. Public methods take no token. Tokens are supplied explicitly and never automatically replaced: retain the new grant after reconnecting. Agent-session grants can be kept separately from adapter sessions.

Every endpoint returns `true, decodedResponse` or `false, ApiError`. Errors contain `code`, `message`, `retryable`, and an optional `status_code`. HTTP and JSON failures are caught; no automatic retry is performed. Responses are checked to be JSON tables; exported types describe the server contract, not full runtime schema validation.

`get_events(token, agent_id, options?)` and `get_audit(token, options?)` accept `{ after = 0, limit = 100 }`. The server requires a nonnegative integer cursor and a limit from 1 to 100.

Request types mark server-defaulted fields optional. Use JSON-compatible values, not Vector3/Instance/CFrame values. Required object fields such as observation `data` encode empty tables as `{}`; known list fields encode empty tables as `[]`. In arbitrary nested JSON data, an empty table means an object. Omit optional fields instead of supplying JSON null. Arrays must be dense, and numbers finite.

## Endpoint methods

| Method | HTTP | Route | Body | Returns |
| --- | --- | --- | --- | --- |
| `health` | GET | `/health` | — | `Status` |
| `protocol` | GET | `/sbp/v0.1/protocol` | — | `ProtocolInfo` |
| `create_session` | POST | `/sbp/v0.1/sessions` | `SessionRequest` | `SessionGrant` |
| `reconnect_session` | POST | `/sbp/v0.1/sessions/reconnect` | — | `SessionGrant` |
| `revoke_session` | DELETE | `/sbp/v0.1/sessions/{session_id}` | — | `Status` |
| `revoke_principal` | DELETE | `/sbp/v0.1/principals/{principal_id}` | — | `Status` |
| `list_environments` | GET | `/sbp/v0.1/environments` | — | `{Environment}` |
| `register_environment` | POST | `/sbp/v0.1/environments` | `EnvironmentRequest` | `Environment` |
| `list_agents` | GET | `/sbp/v0.1/agents` | — | `{Agent}` |
| `register_agent` | POST | `/sbp/v0.1/agents` | `AgentRequest` | `Agent` |
| `get_agent` | GET | `/sbp/v0.1/agents/{agent_id}` | — | `Agent` |
| `set_agent_state` | PATCH | `/sbp/v0.1/agents/{agent_id}` | `AgentState` | `Agent` |
| `create_agent_session` | POST | `/sbp/v0.1/agents/{agent_id}/sessions` | — | `SessionGrant` |
| `submit_action` | POST | `/sbp/v0.1/agents/{agent_id}/actions` | `ActionRequest` | `Action` |
| `get_action` | GET | `/sbp/v0.1/agents/{agent_id}/actions/{action_id}` | — | `Action` |
| `cancel_action` | POST | `/sbp/v0.1/agents/{agent_id}/actions/{action_id}/cancel` | — | `Action` |
| `poll_actions` | GET | `/sbp/v0.1/environments/{environment_id}/actions` | — | `{Action}` |
| `update_action` | POST | `/sbp/v0.1/environments/{environment_id}/agents/{agent_id}/actions/{action_id}` | `ActionUpdate` | `Action` |
| `observe` | POST | `/sbp/v0.1/environments/{environment_id}/observations` | `ObservationRequest` | `Event` |
| `get_events` | GET | `/sbp/v0.1/agents/{agent_id}/events` | — | `EventPage` |
| `send_message` | POST | `/sbp/v0.1/messages` | `MessageRequest` | `Event` |
| `send_agent_message` | POST | `/sbp/v0.1/agents/{agent_id}/messages` | `MessageRequest` | `Event` |
| `set_knowledge_sources` | PUT | `/sbp/v0.1/agents/{agent_id}/knowledge-sources` | `{KnowledgeSource}` | `Agent` |
| `put_knowledge` | POST | `/sbp/v0.1/agents/{agent_id}/knowledge` | `KnowledgeRequest` | `Knowledge` |
| `list_knowledge` | GET | `/sbp/v0.1/agents/{agent_id}/knowledge` | — | `{Knowledge}` |
| `run_task` | POST | `/sbp/v0.1/agents/{agent_id}/tasks` | `TaskRequest` | `Task` |
| `list_tasks` | GET | `/sbp/v0.1/agents/{agent_id}/tasks` | — | `{Task}` |
| `get_audit` | GET | `/sbp/v0.1/audit` | — | `{AuditEntry}` |

## Validation

Rojo build passed. All endpoint/method pairs were compared with `Ai/universal/http.py`. A mocked HTTP smoke check exercised all 28 wrappers, headers, path escaping, pagination, empty object/list serialization, invalid JSON, HTTP diagnostics, and transport errors. The smoke check used Lua with Luau annotations removed; it does not replace a Luau type check or a Roblox Studio integration test. No live API calls were made.
