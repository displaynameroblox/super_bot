local ReplicatedStorage = game:GetService("ReplicatedStorage")
local self = {}

--[[
    for now, there is no complx api calling metohsm, this is meant for making the AI control the charpacter,
    and think about the space around it!
]]

local _AI_tasks = "You a AI friend to this player ".. game:GetService("Players").LocalPlayer.DisplayName .. ", you can chat with him, walk with him and say stories to eachoter. you start following the player, simply return a string like this '##follow_main_player !N : string' which the server will make your charactper follow the player, N being how close you want to follow the player based how close both of you are! to chat with him, simply issue the command ##Say !M : string, where M is your message!"
local _AI_presona = "A friendly and an active guy, you like to talk about anything with friends. you like to read and talk about militay and cold war jets and airal warfare." --why?

local char_handler = require(script.Parent:WaitForChild("char_handler"))
local chat_handler = require(script.Parent:WaitForChild("chat_handler"))
local nextRequestId = 0

local cmds = {
    ["follow_main_player"] = function()
        return char_handler.start_follow(game:GetService("Players").LocalPlayer)
    end,
    ["Say"] = function(message)
        local characterOk, character, characterError = char_handler.getchar()
        if not characterOk or not character then
            return false, characterError or "No NPC character is available"
        end
        return chat_handler.say(message, character)
    end
}

function self:start_up() : (boolean, string?)
    local data_link16 : RemoteEvent = ReplicatedStorage.Folder:WaitForChild("data_link") -- data link 16
    data_link16.OnClientEvent:Connect(function(cmd : string)
        if not cmd then
            warn("Server fired an event but without a commad! got  ".. tostring(typeof(cmd)).."")
        end
        local sub = string.sub(cmd,1,2)
        if sub == "!#" then
            -- to be done later
        elseif sub == "##" then
            local s,r = pcall(self:cmd(cmd))
            if not s then
                warn("[API_HANDLER]: ERROR while running command ".. tostring(cmd) .." sent by server \n ".. tostring(r) .."")
            end
        end
    end)
    return true
end

function self:sent_server_command(cmd : string) : (boolean, string?)
    if typeof(cmd) ~= "string" then
        return false, "Command must be a string"
    end

    local data_link16 : RemoteEvent = ReplicatedStorage.Folder:WaitForChild("data_link")
    nextRequestId += 1
    local requestId = nextRequestId
    local responseEvent = Instance.new("BindableEvent")
    local responseReceived = false
    local responseSucceeded
    local responseError
    local responseConnection = data_link16.OnClientEvent:Connect(function(responseType, responseRequestId, succeeded, responseMessage)
        if responseType == "!#API_RESULT" and responseRequestId == requestId then
            responseReceived = true
            responseSucceeded = succeeded
            responseError = responseMessage
            responseEvent:Fire()
        end
    end)

    local callSucceeded, commandError = pcall(function()
        data_link16:FireServer(cmd, requestId)
    end)

    if not callSucceeded then
        responseConnection:Disconnect()
        responseEvent:Destroy()
        return false, tostring(commandError)
    end

    task.delay(15, function()
        if not responseReceived then
            responseReceived = true
            responseSucceeded = false
            responseError = "Timed out waiting for the server API response"
            responseEvent:Fire()
        end
    end)

    if not responseReceived then
        responseEvent.Event:Wait()
    end
    responseConnection:Disconnect()
    responseEvent:Destroy()
    if responseSucceeded ~= true then
        return false, tostring(responseError or "The server rejected the command")
    end
    return true
end

function self:sent_server_commad(cmd : string) : (boolean, string?)
    return self:sent_server_command(cmd)
end

function self:cmd(s : string)
    if not s or typeof(s) ~= "string" then
        return s, "You failed to run a command! did you forget return a string?"
    end

    local commandName, argument = string.match(s, "^##([%w_]+)%s+![%w_]+=(.-)%s*$")
    if not commandName then
        return false, "Invalid command syntax. Expected ##Command !Argument=value"
    end

    local command = cmds[commandName]
    if not command then
        return false, "Unknown command: " .. commandName
    end

    local quote = string.sub(argument, 1, 1)
    if (quote == "'" or quote == '"') and string.sub(argument, -1) == quote then
        argument = string.sub(argument, 2, -2)
    end

    local callSucceeded, result, commandError = pcall(command, argument)
    if not callSucceeded then
        return false, "Your command failed to run: " .. tostring(result)
    end
    if result == false then
        return false, commandError
    end
    return true, commandError
end

return self