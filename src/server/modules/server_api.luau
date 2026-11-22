-- SBP v0.1 HTTP client and existing Roblox command dispatcher.
local HttpService = game:GetService("HttpService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local self = {}

export type JsonValue = boolean | number | string | {JsonValue} | {[string]: JsonValue}
export type KnowledgeSource = "environment" | "experience" | "developer" | "user" | "shared" | "memory"

export type Action = {
    id: string,
    environment_id: string,
    capability: string,
    version: string,
    input: {[string]: JsonValue},
    agent_id: string,
    state: "queued" | "acknowledged" | "running" | "cancel_requested" | "completed" | "failed" | "cancelled",
    progress: number,
    result: JsonValue?,
    error: Diagnostic | nil,
    created_at: number,
    deadline: number,
    capability_snapshot: Capability,
}

export type ActionRequest = {
    id: string,
    environment_id: string,
    capability: string,
    version: string,
    input: ({[string]: JsonValue})?,
}

export type ActionUpdate = {
    state: "acknowledged" | "running" | "completed" | "failed" | "cancelled",
    progress: (number)?,
    result: (JsonValue)?,
    error: Diagnostic | nil,
}

export type Agent = {
    id: string,
    name: string,
    environments: {string},
    capabilities: {string},
    knowledge_sources: {"environment" | "experience" | "developer" | "user" | "shared" | "memory"},
    owner: string,
    status: "active" | "paused" | "stopped",
}

export type AgentRequest = {
    id: string,
    name: string,
    environments: ({string})?,
    capabilities: ({string})?,
    knowledge_sources: ({"environment" | "experience" | "developer" | "user" | "shared" | "memory"})?,
}

export type AgentState = {
    status: "active" | "paused" | "stopped",
}

export type AuditEntry = {
    sequence: number,
    time: number,
    principal_id: string,
    operation: string,
    resource: string,
    outcome: string,
}

export type Capability = {
    id: string,
    version: string,
    input_schema: {[string]: JsonValue},
    output_schema: {[string]: JsonValue},
    permissions: {string},
    constraints: Constraints,
    status: "available" | "unavailable",
    cancellation: boolean,
}

export type Constraints = {
    timeout_seconds: number,
    max_concurrent: number,
}

export type Diagnostic = {
    code: string,
    message: string,
    retryable: boolean,
}

export type Environment = {
    id: string,
    name: string,
    kind: string,
    capabilities: {Capability},
    owner: string,
}

export type EnvironmentRequest = {
    id: string,
    name: string,
    kind: string,
    capabilities: ({Capability})?,
}

export type Event = {
    sequence: number,
    type: string,
    agent_id: string,
    environment_id: string | nil,
    time: number,
    data: {[string]: JsonValue},
}

export type EventPage = {
    events: {Event},
    next_cursor: number,
}

export type Knowledge = {
    source: "environment" | "experience" | "developer" | "user" | "shared" | "memory",
    content: string,
    provenance: string,
    confidence: number,
    verified: boolean,
    scope: "private" | "shared" | "environment",
    environment_id: string | nil,
    shared_with: {string},
    id: string,
    owner: string,
    agent_id: string,
    created_at: number,
}

export type KnowledgeRequest = {
    source: "environment" | "experience" | "developer" | "user" | "shared" | "memory",
    content: string,
    provenance: string,
    confidence: (number)?,
    verified: (boolean)?,
    scope: ("private" | "shared" | "environment")?,
    environment_id: string | nil,
    shared_with: ({string})?,
}

export type MessageRequest = {
    recipient: string,
    text: string,
}

export type ObservationRequest = {
    id: string,
    agent_id: string,
    kind: string,
    data: {[string]: JsonValue},
    observed_at: number,
}

export type ProtocolInfo = {
    versions: {string},
    transports: {string},
}

export type SessionGrant = {
    session_id: string,
    version: "0.1",
    access_token: string,
    expires_at: number,
    principal_id: string,
    agent_id: string | nil,
}

export type SessionRequest = {
    versions: ({string})?,
}

export type Status = {
    status: string,
}

export type Task = {
    id: string,
    agent_id: string,
    goal: string,
    state: "running" | "waiting" | "completed" | "failed",
    action_ids: {string},
    response: string | nil,
    error: Diagnostic | nil,
}

export type TaskRequest = {
    goal: string,
    model: string | nil,
    actions: ({ActionRequest})?,
}

export type ApiError = { code: string, message: string, retryable: boolean, status_code: number? }
export type PageOptions = { after: number?, limit: number? }
local baseUrl: string? = nil
local PREFIX = "/sbp/v0.1"

local bodyShapes: {[string]: any} = {
    ActionRequest = { fields = { ["input"] = { fields = {  } } } },
    ActionUpdate = { fields = { ["error"] = { fields = {  } } } },
    AgentRequest = { fields = { ["environments"] = { array = true, item = nil }, ["capabilities"] = { array = true, item = nil }, ["knowledge_sources"] = { array = true, item = nil } } },
    AgentState = { fields = {  } },
    EnvironmentRequest = { fields = { ["capabilities"] = { array = true, item = { fields = { ["input_schema"] = { fields = {  } }, ["output_schema"] = { fields = {  } }, ["permissions"] = { array = true, item = nil }, ["constraints"] = { fields = {  } } } } } } },
    KnowledgeRequest = { fields = { ["shared_with"] = { array = true, item = nil } } },
    MessageRequest = { fields = {  } },
    ObservationRequest = { fields = { ["data"] = { fields = {  } } } },
    SessionRequest = { fields = { ["versions"] = { array = true, item = nil } } },
    TaskRequest = { fields = { ["actions"] = { array = true, item = { fields = { ["input"] = { fields = {  } } } } } } },
    Sources = { array = true },
}

-- Explicit request shapes preserve [] versus {} for empty tables.
-- In arbitrary JSON data, empty tables mean {}; omit optional fields to send no value.
local function encode(value: any, shape: any): string
    if type(value) == "number" then
        assert(value == value and math.abs(value) ~= math.huge, "JSON numbers must be finite")
    end
    if type(value) ~= "table" then
        return HttpService:JSONEncode(value)
    end
    local isArray = (shape and shape.array) or (not shape and #value > 0)
    local parts = {}
    if isArray then
        for key in pairs(value) do
            assert(type(key) == "number" and key % 1 == 0 and key >= 1 and key <= #value, "JSON arrays must be dense lists")
        end
        for _, item in ipairs(value) do
            table.insert(parts, encode(item, shape and shape.item))
        end
        return "[" .. table.concat(parts, ",") .. "]"
    end
    for key, item in pairs(value) do
        assert(type(key) == "string", "JSON objects require string keys")
        table.insert(parts, HttpService:JSONEncode(key) .. ":" .. encode(item, shape and shape.fields and shape.fields[key]))
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

function self.configure(url: string)
    assert(type(url) == "string" and string.match(url, "^https://[^/]+"), "SBP requires an HTTPS base URL")
    assert(not string.find(url, "[?#]"), "Base URL must not contain a query or fragment")
    baseUrl = string.gsub(url, "/+$", "")
end

local function failure(code: string, message: string, status: number?, retryable: boolean?): (boolean, any)
    return false, { code = code, message = message, status_code = status, retryable = retryable == true }
end

local function request(method: string, path: string, token: string?, body: any?, shapeName: string?): (boolean, any)
    if not baseUrl then
        return failure("not_configured", "Call server_api.configure(baseUrl) first")
    end
    if token ~= nil and token == "" then
        return failure("missing_token", "A nonempty bearer token is required")
    end
    local options: any = {
        Url = baseUrl .. path,
        Method = method,
        Headers = { Accept = "application/json" },
    }
    if token then options.Headers.Authorization = "Bearer " .. token end
    if body ~= nil then
        local encoded, payload = pcall(encode, body, shapeName and bodyShapes[shapeName])
        if not encoded then return failure("invalid_request", tostring(payload)) end
        options.Headers["Content-Type"] = "application/json"
        options.Body = payload
    end
    local sent, response = pcall(function()
        return HttpService:RequestAsync(options)
    end)
    if not sent then return failure("transport_error", tostring(response)) end
    local decoded, data = pcall(function()
        return HttpService:JSONDecode(response.Body)
    end)
    if not response.Success then
        local diagnostic = decoded and type(data) == "table" and data.error
        if type(diagnostic) == "table" and type(diagnostic.code) == "string" and type(diagnostic.message) == "string" then
            return failure(diagnostic.code, diagnostic.message, response.StatusCode, diagnostic.retryable)
        end
        return failure("http_error", response.StatusMessage, response.StatusCode)
    end
    if not decoded or type(data) ~= "table" then
        return failure("invalid_response", "Expected a JSON object or array", response.StatusCode)
    end
    return true, data
end

local function segment(id: string): string
    return HttpService:UrlEncode(id)
end

local function page(options: PageOptions?): string
    local after = options and options.after or 0
    local limit = options and options.limit or 100
    return "?after=" .. segment(tostring(after)) .. "&limit=" .. segment(tostring(limit))
end

-- GET /health
function self:health(): (boolean, Status | ApiError)
    return request("GET", "/health", nil, nil, nil)
end

-- GET /sbp/v0.1/protocol
function self:protocol(): (boolean, ProtocolInfo | ApiError)
    return request("GET", PREFIX .. "/protocol", nil, nil, nil)
end

-- POST /sbp/v0.1/sessions
function self:create_session(token: string, body: SessionRequest): (boolean, SessionGrant | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/sessions", token, body, "SessionRequest")
end

-- POST /sbp/v0.1/sessions/reconnect
function self:reconnect_session(token: string): (boolean, SessionGrant | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/sessions/reconnect", token, nil, nil)
end

-- DELETE /sbp/v0.1/sessions/{session_id}
function self:revoke_session(token: string, session_id: string): (boolean, Status | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("DELETE", PREFIX .. "/sessions/" .. segment(session_id) .. "", token, nil, nil)
end

-- DELETE /sbp/v0.1/principals/{principal_id}
function self:revoke_principal(token: string, principal_id: string): (boolean, Status | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("DELETE", PREFIX .. "/principals/" .. segment(principal_id) .. "", token, nil, nil)
end

-- GET /sbp/v0.1/environments
function self:list_environments(token: string): (boolean, {Environment} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/environments", token, nil, nil)
end

-- POST /sbp/v0.1/environments
function self:register_environment(token: string, body: EnvironmentRequest): (boolean, Environment | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/environments", token, body, "EnvironmentRequest")
end

-- GET /sbp/v0.1/agents
function self:list_agents(token: string): (boolean, {Agent} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents", token, nil, nil)
end

-- POST /sbp/v0.1/agents
function self:register_agent(token: string, body: AgentRequest): (boolean, Agent | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents", token, body, "AgentRequest")
end

-- GET /sbp/v0.1/agents/{agent_id}
function self:get_agent(token: string, agent_id: string): (boolean, Agent | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents/" .. segment(agent_id) .. "", token, nil, nil)
end

-- PATCH /sbp/v0.1/agents/{agent_id}
function self:set_agent_state(token: string, agent_id: string, body: AgentState): (boolean, Agent | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("PATCH", PREFIX .. "/agents/" .. segment(agent_id) .. "", token, body, "AgentState")
end

-- POST /sbp/v0.1/agents/{agent_id}/sessions
function self:create_agent_session(token: string, agent_id: string): (boolean, SessionGrant | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/sessions", token, nil, nil)
end

-- POST /sbp/v0.1/agents/{agent_id}/actions
function self:submit_action(token: string, agent_id: string, body: ActionRequest): (boolean, Action | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/actions", token, body, "ActionRequest")
end

-- GET /sbp/v0.1/agents/{agent_id}/actions/{action_id}
function self:get_action(token: string, agent_id: string, action_id: string): (boolean, Action | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents/" .. segment(agent_id) .. "/actions/" .. segment(action_id) .. "", token, nil, nil)
end

-- POST /sbp/v0.1/agents/{agent_id}/actions/{action_id}/cancel
function self:cancel_action(token: string, agent_id: string, action_id: string): (boolean, Action | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/actions/" .. segment(action_id) .. "/cancel", token, nil, nil)
end

-- GET /sbp/v0.1/environments/{environment_id}/actions
function self:poll_actions(token: string, environment_id: string): (boolean, {Action} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/environments/" .. segment(environment_id) .. "/actions", token, nil, nil)
end

-- POST /sbp/v0.1/environments/{environment_id}/agents/{agent_id}/actions/{action_id}
function self:update_action(token: string, environment_id: string, agent_id: string, action_id: string, body: ActionUpdate): (boolean, Action | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/environments/" .. segment(environment_id) .. "/agents/" .. segment(agent_id) .. "/actions/" .. segment(action_id) .. "", token, body, "ActionUpdate")
end

-- POST /sbp/v0.1/environments/{environment_id}/observations
function self:observe(token: string, environment_id: string, body: ObservationRequest): (boolean, Event | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/environments/" .. segment(environment_id) .. "/observations", token, body, "ObservationRequest")
end

-- GET /sbp/v0.1/agents/{agent_id}/events
function self:get_events(token: string, agent_id: string, options: PageOptions?): (boolean, EventPage | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents/" .. segment(agent_id) .. "/events" .. page(options), token, nil, nil)
end

-- POST /sbp/v0.1/messages
function self:send_message(token: string, body: MessageRequest): (boolean, Event | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/messages", token, body, "MessageRequest")
end

-- POST /sbp/v0.1/agents/{agent_id}/messages
function self:send_agent_message(token: string, agent_id: string, body: MessageRequest): (boolean, Event | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/messages", token, body, "MessageRequest")
end

-- PUT /sbp/v0.1/agents/{agent_id}/knowledge-sources
function self:set_knowledge_sources(token: string, agent_id: string, body: {KnowledgeSource}): (boolean, Agent | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("PUT", PREFIX .. "/agents/" .. segment(agent_id) .. "/knowledge-sources", token, body, "Sources")
end

-- POST /sbp/v0.1/agents/{agent_id}/knowledge
function self:put_knowledge(token: string, agent_id: string, body: KnowledgeRequest): (boolean, Knowledge | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/knowledge", token, body, "KnowledgeRequest")
end

-- GET /sbp/v0.1/agents/{agent_id}/knowledge
function self:list_knowledge(token: string, agent_id: string): (boolean, {Knowledge} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents/" .. segment(agent_id) .. "/knowledge", token, nil, nil)
end

-- POST /sbp/v0.1/agents/{agent_id}/tasks
function self:run_task(token: string, agent_id: string, body: TaskRequest): (boolean, Task | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("POST", PREFIX .. "/agents/" .. segment(agent_id) .. "/tasks", token, body, "TaskRequest")
end

-- GET /sbp/v0.1/agents/{agent_id}/tasks
function self:list_tasks(token: string, agent_id: string): (boolean, {Task} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/agents/" .. segment(agent_id) .. "/tasks", token, nil, nil)
end

-- GET /sbp/v0.1/audit
function self:get_audit(token: string, options: PageOptions?): (boolean, {AuditEntry} | ApiError)
    if type(token) ~= "string" or token == "" then return failure("missing_token", "A bearer token is required") end
    return request("GET", PREFIX .. "/audit" .. page(options), token, nil, nil)
end

local cmds = {
    ["Test"] = function(player, _argument)
        local data_link = ReplicatedStorage:WaitForChild("Folder"):WaitForChild("data_link")
        data_link:FireAllClients("##Say !M='Test from server!!'")
        task.wait(5)
        data_link:FireAllClients("##follow_main_player")
        return true
    end
}

function self.issue_cmd(cmd : string, player : Player)
    if typeof(cmd) ~= "string" then
        warn("[API_SERVER]: Expected a string command, got " .. tostring(typeof(cmd)))
        return false, "INVALID_CMD"
    end

    local commandName, argument = string.match(cmd, "^##([%w_]+)%s+!%w+=(.-)%s*$")
    if not commandName then
        warn("[API_SERVER]: Invalid command syntax. Expected ##Command !Argument=value")
        return false, "INVALID_CMD"
    end

    local command = cmds[commandName]
    if not command then
        warn("[API_SERVER]: Unknown command issued: " .. tostring(commandName))
        return false, "UNKNOWN_CMD"
    end

    if argument then
        local quote = string.sub(argument, 1, 1)
        if (quote == "'" or quote == '"') and string.sub(argument, -1) == quote then
            argument = string.sub(argument, 2, -2)
        end
    end

    local callSucceeded, result, commandError = pcall(command, player, argument)
    if not callSucceeded then
        warn("[API_SERVER]: Failed to run command: " .. tostring(commandName) .. "\n" .. tostring(result))
        return false, tostring(result)
    end

    if result == false then
        return false, tostring(commandError or "Command returned false")
    end

    return true, commandError
end

function self:start_up()
    local data_link16 = ReplicatedStorage:WaitForChild("Folder"):WaitForChild("data_link")
    data_link16.OnServerEvent:Connect(function(player: Player, cmd: string, requestId: number?)
        local succeeded, commandError = self.issue_cmd(cmd, player)
        if requestId then
            data_link16:FireClient(player, "!#API_RESULT", requestId, succeeded, commandError)
        end
    end)
end

return self
