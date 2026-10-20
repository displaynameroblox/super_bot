--!nocheck
local self = {}

local mem = {}
local config = {
    stop_distance_studs = 1 -- like the fuck?
}
local Players = game:GetService("Players")
local rs = game:GetService("RunService")

function self.makechar(): (boolean, Model?, string?)
    if mem.char and mem.char.Parent then
        return true, mem.char
    end

    local success, result = pcall(function()
        local character = game.ReplicatedStorage.char:Clone()
        character:PivotTo(CFrame.new(0, 10, 0))
        character.Parent = workspace
        return character
    end)

    if not success then
        return false, nil, "Failed to clone avatar: " .. tostring(result)
    end

    if typeof(result) ~= "Instance" or not result:IsA("Model") then
        return false, nil, "Failed to clone avatar: clone did not produce a Model"
    end

    mem.char = result
    return true, mem.char
end

function self.getchar(): (boolean, Model?, string?)
    if mem.char and mem.char.Parent then
        return true, mem.char
    end

    return self.makechar()
end

function self.movechar(vec3: Vector3): (boolean, string?)
    warn("[char_handler]: this is no longer used and will be removed later, use GoToPart()!")
    return false, "movechar() is deprecated; use GoToPart() instead"
end

local following = false
local followToken = 0 
local followingHumanoid: Humanoid? = nil
local followingRoot: BasePart? = nil

function self.stop_follow()
    following = false
    followToken += 1
    if followingHumanoid and followingHumanoid.Parent and followingRoot and followingRoot.Parent then
        followingHumanoid:MoveTo(followingRoot.Position)
    end
    followingHumanoid = nil
    followingRoot = nil
end

function self.GoToPart(target: BasePart | Player): (boolean, string?)
    if typeof(target) ~= "Instance" then
        return false, "Target must be a BasePart or Player"
    end

    local supportedTarget = target:IsA("BasePart") or target:IsA("Player")
    if not supportedTarget then
        return false, "Target must be a BasePart or Player"
    end

    if target:IsA("BasePart") then
        local characterOk, character, characterError = self.getchar()
        if not characterOk or not character then
            return false, characterError or "No NPC character is available"
        end

        local humanoid = character:FindFirstChildOfClass("Humanoid")
        if not humanoid then
            return false, "Cannot find a Humanoid in the NPC character"
        end

        local npcRoot = character:FindFirstChild("HumanoidRootPart")
        if not npcRoot or not npcRoot:IsA("BasePart") then
            return false, "Cannot find a HumanoidRootPart in the NPC character"
        end

        if (target.Position - npcRoot.Position).Magnitude <= config.stop_distance_studs then
            return true
        end

        humanoid:MoveTo(target.Position)
        local reached = humanoid.MoveToFinished:Wait()
        if not reached then
            return false, "NPC failed to reach the target part"
        end
        return true
    end

    return self.start_follow(target :: Player)
end

function self.start_follow(player: Player): (boolean, string?)
    if typeof(player) ~= "Instance" or not player:IsA("Player") then
       warn("no player passed!")
       player = Players.LocalPlayer
    end

    if following then
        return false, "NPC is already following a player"
    end

    local characterOk, character, characterError = self.getchar()
    if not characterOk or not character then
        return false, characterError or "No NPC character is available"
    end

    local humanoid = character:FindFirstChildOfClass("Humanoid")
    if not humanoid then
        return false, "Cannot find a Humanoid in the NPC character"
    end

    local npcRoot = character:FindFirstChild("HumanoidRootPart")
    if not npcRoot or not npcRoot:IsA("BasePart") then
        return false, "Cannot find a HumanoidRootPart in the NPC character"
    end

    following = true
    followToken += 1
    local runToken = followToken
    followingHumanoid = humanoid
    followingRoot = npcRoot
    local followError: string? = nil
    local lastMoveAt = 0
    local isMoving = false

    while following and followToken == runToken do
        if not player.Parent then
            followError = "Player left the game"
            break
        end
        if not humanoid.Parent or not npcRoot.Parent then
            followError = "NPC character was removed while following"
            break
        end

        local playerCharacter = player.Character
        local playerRoot = playerCharacter and playerCharacter:FindFirstChild("HumanoidRootPart")
        if playerRoot and playerRoot:IsA("BasePart") then
            local distance = (playerRoot.Position - npcRoot.Position).Magnitude
            if distance <= config.stop_distance_studs then
                if isMoving then
                    humanoid:MoveTo(npcRoot.Position)
                    isMoving = false
                end
            elseif not isMoving or os.clock() - lastMoveAt >= 0.5 then
                humanoid:MoveTo(playerRoot.Position)
                lastMoveAt = os.clock()
                isMoving = true
            end
        elseif isMoving then
            humanoid:MoveTo(npcRoot.Position)
            isMoving = false
        end

        rs.Heartbeat:Wait()
    end

    if followToken == runToken then
        self.stop_follow()
        following = false
    end
    if followError then
        return false, followError
    end
    return true
end

return self