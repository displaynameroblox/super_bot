local EncodingService = game:GetService("EncodingService")
local HttpService = game:GetService("HttpService")
local self = {}

local data_link = game.ReplicatedStorage.Folder.data_link
local running_port = "8080"
local api_key = "fuck you tunsi"
local url
local session_grant

local environment = {
    id = "game",
    name = "SDK development environment",
    kind = "game",
    capabilities = {
        {
            id = "navigation.move_to",
            version = "1",
            input_schema = {
                type = "object",
                properties = {
                    x = { type = "number" },
                },
                required = { "x" },
                additionalProperties = false,
            },
            output_schema = {
                type = "object",
                properties = {
                    arrived = { type = "boolean" },
                },
                required = { "arrived" },
                additionalProperties = false,
            },
            permissions = { "move" },
            constraints = {
                timeout_seconds = 60,
                max_concurrent = 8,
            },
            status = "available",
            cancellation = false,
        },
    },
}

local explorer = {
    id = "explorer",
    name = "Explorer",
    environments = { "game" },
    capabilities = { "navigation.move_to" },
    knowledge_sources = {
        "environment",
        "experience",
        "developer",
        "user",
        "shared",
        "memory",
    },
}

type SessionGrant = {
    access_token: string,
    session_id: string,
    version: string,
    expires_at: number,
    principal_id: string,
    agent_id: string?,
}

type KnowledgeSource =
    "environment"
    | "experience"
    | "developer"
    | "user"
    | "shared"
    | "memory"

type agent = {
    id: string,
    name: string,
    environments: {string},
    capabilities: {string},
    knowledge_sources: {KnowledgeSource},
    owner: string,
    status: "active" | "paused" | "stopped",
}

export type JsonValue =
    boolean | number | string | {JsonValue} | {[string]: JsonValue}

export type ObservationRequest = {
    id: string,
    agent_id: string,
    kind: string,
    data: {[string]: JsonValue},
    observed_at: number,
}

export type ObservationEvent = {
    sequence: number,
    type: "observation",
    agent_id: string,
    environment_id: string,
    time: number,
    data: ObservationRequest,
}

function self:sbp_v01_observations(
    base_url: string,
    access_token: string,
    observation: ObservationRequest
): (boolean, string | ObservationEvent)
    local requested, response = pcall(function()
        return HttpService:RequestAsync({
            Url = base_url:gsub("/+$", "")
                .. "/sbp/v0.1/environments/game/observations",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
                ["Authorization"] = "Bearer " .. access_token,
            },
            Body = HttpService:JSONEncode(observation),
        })
    end)

    if not requested then
        return false, tostring(response)
    end

    if not response.Success then
        return false, "HTTP " .. tostring(response.StatusCode)
            .. ": " .. response.StatusMessage
            .. "\n" .. response.Body
    end

    local decoded, event = pcall(function()
        return HttpService:JSONDecode(response.Body)
    end)

    if not decoded then
        return false, "Server returned invalid JSON"
    end

    if type(event) ~= "table"
        or type(event.sequence) ~= "number"
        or event.type ~= "observation"
        or event.agent_id ~= observation.agent_id
        or event.environment_id ~= "game"
        or type(event.time) ~= "number"
        or type(event.data) ~= "table"
        or event.data.id ~= observation.id
        or event.data.agent_id ~= observation.agent_id
        or type(event.data.kind) ~= "string"
        or type(event.data.data) ~= "table"
        or type(event.data.observed_at) ~= "number"
    then
        return false, "Server returned an invalid observation event"
    end

    return true, event
end
`q1

function self:sbp_vo1_agent(name : string, id : string) : (boolean, string? | agent)
    local table = table.clone(explorer)
    if not name or id then
        return false, "malformed input"
    end
    if not table.name or table.id then
        return false, "malformed interal table!"
    end
    local requested, response = pcall(function()
    return HttpService:RequestAsync({
        Url = url:gsub("/+$", "") .. "/sbp/v0.1/agents",
        Method = "POST",
        Headers = {
            ["Content-Type"] = "application/json",
            ["Authorization"] = "Bearer " .. access_token,
        },
        Body = HttpService:JSONEncode(explorer),
    })
end)
    if not requested then
        return false, response
    else
        return true, HttpService.JSONDecode(response)
    end
end

function self:sbp_vo1_environments_game_observations()

function self:sbp_vo1_sessions(): (boolean, string | SessionGrant)
    if not url then
        return false, "Server URL has not been initialized"
    end

    local requested, response = pcall(function()
        return HttpService:RequestAsync({
            Url = url:gsub("/+$", "") .. "/sbp/v0.1/sessions",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
                ["Authorization"] = "Bearer " .. api_key,
            },
            Body = HttpService:JSONEncode({
                versions = { "0.1" },
            }),
        })
    end)

    if not requested then
        return false, tostring(response)
    end

    if not response.Success then
        return false, "HTTP " .. tostring(response.StatusCode)
            .. ": " .. response.StatusMessage
    end

    if not response.Body or response.Body == "" then
        return false, "Server did not return a body!"
    end

    local decoded, data = pcall(function()
        return HttpService:JSONDecode(response.Body)
    end)

    if not decoded then
        return false, "Server returned invalid JSON"
    end

    if type(data) ~= "table"
        or type(data.access_token) ~= "string"
        or type(data.session_id) ~= "string"
        or type(data.version) ~= "string"
        or type(data.expires_at) ~= "number"
        or type(data.principal_id) ~= "string"
        or (data.agent_id ~= nil and type(data.agent_id) ~= "string")
    then
        return false, "Server returned an invalid session response"
    end

    return true, data
end

function self:sbp_v01_env() :(boolean,string? | {
    owner: string,
    kind: string
})
     local requested, response = pcall(function()
        return HttpService:RequestAsync({
            Url = url:gsub("/+$", "") .. "/sbp/v0.1/environments",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
            },
            Body = HttpService:JSONEncode({
                environment
            }),
        })
    end)
    if not requested then
        return false, tostring(response)
    end
    return true, response.StatusCode
end

function self:ping() : (boolean,string? | {
    Success: boolean,
    StatusCode: number,
    StatusMessage: string,
    Headers: {[string]: string},
    body: string
} )
    local pinged, result = pcall(HttpService.RequestAsync(
        {
            Url = url .. "/health",
            Method = "POST"
        }
    ))

    if not pinged then
        warn("Failed to ping ".. url .."! error: ".. tostring(result) .." ")
        return false, result
    else
        return true, result
    end
end

function self:start_up(URL)
    if HttpService.HttpEnabled == false then
        error("Can't reach local server, HttpService.HttpEnabled is set to false")
    end
    if not URL then
        error("no url")
    end
    url = URL
    local pinged, string = self:ping()
    if not pinged then
        error("Failed to check server! ping() returned: \n ".. string)
    else
        print("server found!")
    end
    print("Checking protocal..")
    warn("no protocool checking function as of 2:00 AM nov 6 2026 ") --NNN
    
    print("authenticating session..")
    local checked, session = self:sbp_vo1_sessions()
    if not checked then
        error("Failed to check sessions, error: "..session)
    else
        session_grant = session
        if typeof(session_grant) == "SessionGrant" then
            print("session saved!")
        end
    end
    print("Registering..")
    local registered, register = self:sbp_vo1_environments()
    if not registered then
        error("failed registe the environment")
    end
    print("server connecting finished, starting super-bot roblox SDK v0.0.0.a1 ...")
    warn("as of 2:15 AM nov 4 2026, there is no function to check")
    
    print("Finished loading SDK",
          "Start agents to make use the SDK!",
          "Using version 0.1 for server side SDK, v0.0.0a.1 for roblox side SDK ..."
)
    return true -- nothing yet
end

function start_ses()
    
end

function self:test(player : Player)
    if not player then
        warn("no player was passed! the result may be inaccrnet!")
    end
    data_link:FireAllClients("##Say !M='Test from server!!'")
    task.wait(5)
    data_link:FireAllClients("##follow_main_player")
end



return self