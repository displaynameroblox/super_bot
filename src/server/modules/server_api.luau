local ReplicatedStorage = game:GetService("ReplicatedStorage")
local ai_handler = require(script.Parent.ai_handler)
local self = {}

local data_link16 = ReplicatedStorage.Folder:WaitForChild("data_link")

if not data_link16 then
    error("FAIL: no data link found")
end

local cmds = {
    ["Test"] = function(player, _argument)
        ai_handler:test(player)
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
    data_link16.OnServerEvent:Connect(function(player: Player, cmd: string, requestId: number?)
        local succeeded, commandError = self.issue_cmd(cmd, player)
        if requestId then
            data_link16:FireClient(player, "!#API_RESULT", requestId, succeeded, commandError)
        end
    end)
end

return self