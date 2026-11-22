--!strict
-- Require this module from a server Script. Keep one instance per approved player.
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local TextService = game:GetService("TextService")

assert(RunService:IsServer(), "Miso SDK must run on the server")

local SDK = {}

export type Failure = {
    code: string,
    status: number?,
    retryable: boolean,
}

local function failure(code: string, status: number?, retryable: boolean): Failure
    return { code = code, status = status, retryable = retryable }
end

function SDK.new(origin: string, secretName: string, player: Player)
    -- A bare conventional HTTPS DNS origin keeps paths and credentials under SDK control.
    assert(string.match(origin, "^https://[%w%-%.]+%.[%a]+$"), "Expected a public HTTPS origin")
    assert(player.Parent == Players and player.UserId > 0, "Expected a connected Roblox player")
    assert(#secretName > 0, "Expected a Roblox secret name")

    local sessionId = HttpService:GenerateGUID(false)
    local busy = false
    local nextChatAt = 0

    local function request(action: string, payload: {[string]: string}): (any, Failure?)
        if not HttpService.HttpEnabled then
            return nil, failure("http_disabled", nil, false)
        end
        if player.Parent ~= Players then
            return nil, failure("player_disconnected", nil, false)
        end
        local secretOk, authorization = pcall(function()
            return HttpService:GetSecret(secretName):AddPrefix("Bearer ")
        end)
        if not secretOk then
            return nil, failure("secret_unavailable", nil, false)
        end
        local ok, response = pcall(function()
            return HttpService:RequestAsync({
                Url = origin .. "/miso/roblox/" .. action,
                Method = "POST",
                Headers = {
                    ["Content-Type"] = "application/json",
                    ["Authorization"] = authorization,
                },
                Body = HttpService:JSONEncode(payload),
            })
        end)
        if not ok then
            return nil, failure("network_error", nil, true)
        end
        if not response.Success then
            local status = response.StatusCode
            return nil, failure("bridge_rejected", status, status == 409 or status == 429 or status >= 500)
        end
        local decodedOk, decoded = pcall(function()
            return HttpService:JSONDecode(response.Body)
        end)
        if not decodedOk or type(decoded) ~= "table" then
            return nil, failure("invalid_response", nil, false)
        end
        return decoded, nil
    end

    local client = {}

    function client:status(): (boolean, Failure?)
        local result, err = request("status", {
            universe_id = tostring(game.GameId),
            player_id = tostring(player.UserId),
        })
        if err then
            return false, err
        end
        if result.status ~= "ok" or result.integration ~= "miso-roblox-v1" then
            return false, failure("invalid_response", nil, false)
        end
        return true, nil
    end

    function client:chat(text: string, previousRequestId: string?): (string?, Failure?, string?)
        if type(text) ~= "string" or #text > 1800 or not string.find(text, "%S") then
            return nil, failure("invalid_text", nil, false), nil
        end
        if busy or os.clock() < nextChatAt then
            return nil, failure("local_cooldown", nil, true), previousRequestId
        end
        local requestId = previousRequestId or HttpService:GenerateGUID(false)
        nextChatAt = os.clock() + 15
        busy = true
        local result, err = request("chat", {
            universe_id = tostring(game.GameId),
            player_id = tostring(player.UserId),
            session_id = sessionId,
            request_id = requestId,
            text = text,
        })
        busy = false
        if err then
            return nil, err, requestId
        end
        if type(result.reply) ~= "string" or #result.reply == 0 or #result.reply > 1600 then
            return nil, failure("invalid_response", nil, false), requestId
        end
        if player.Parent ~= Players then
            return nil, failure("player_disconnected", nil, false), requestId
        end
        -- Display only in that player's private UI, and fail closed if filtering fails.
        local filteredOk, filtered = pcall(function()
            return TextService:FilterStringAsync(result.reply, player.UserId, Enum.TextFilterContext.PrivateChat)
                :GetNonChatStringForUserAsync(player.UserId)
        end)
        if not filteredOk or player.Parent ~= Players then
            return nil, failure("filter_unavailable", nil, false), requestId
        end
        return filtered, nil, requestId
    end

    return client
end

return SDK
