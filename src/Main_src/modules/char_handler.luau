--!nocheck
local self = {}

local mem = {}
local config = {
    stop_distance_studs = 1, -- like the fuck?
    path_find_to_go = true,
    master_debug = true
}
local Players = game:GetService("Players")
local rs = game:GetService("RunService")
local pfs = game:GetService("PathfindingService")

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
local waypointDebugFolder: Folder? = nil

local function clearWaypointDebugMarkers()
    if waypointDebugFolder then
        waypointDebugFolder:Destroy()
        waypointDebugFolder = nil
    end
end

local function showWaypointDebugMarkers(waypoints: {PathWaypoint})
    clearWaypointDebugMarkers()

    if not config.master_debug then
        return
    end

    local folder = Instance.new("Folder")
    folder.Name = "CharHandlerWaypointDebug"
    folder.Parent = workspace
    waypointDebugFolder = folder

    for index, waypoint in ipairs(waypoints) do
        local marker = Instance.new("Part")
        marker.Name = "Waypoint_" .. tostring(index)
        marker.Shape = Enum.PartType.Ball
        marker.Size = Vector3.new(0.8, 0.8, 0.8)
        marker.Position = waypoint.Position + Vector3.new(0, 0.5, 0)
        marker.Anchored = true
        marker.CanCollide = false
        marker.CanTouch = false
        marker.CanQuery = false
        marker.CastShadow = false
        marker.Material = Enum.Material.Neon
        marker.Color = if waypoint.Action == Enum.PathWaypointAction.Jump
            then Color3.fromRGB(255, 170, 0)
            else Color3.fromRGB(0, 200, 255)
        marker.Parent = folder
    end
end

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

    if target:IsA("Player") then
        return self.start_follow(target)
    end

    if not target:IsA("BasePart") then
        return false, "Target must be a BasePart or Player"
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

    if (target.Position - npcRoot.Position).Magnitude <= config.stop_distance_studs then
        clearWaypointDebugMarkers()
        return true
    end

    if config.path_find_to_go then
        for attempt = 1, 3 do
            local path
            local computed, computeError = pcall(function()
                path = pfs:CreatePath({
                    AgentCanClimb = true,
                    Costs = {
                        SAFE_GROUND = 0,
                    },
                })
                path:ComputeAsync(npcRoot.Position, target.Position)
            end)

            if not computed then
                return false, "Failed to compute path: " .. tostring(computeError)
            end
            if path.Status ~= Enum.PathStatus.Success then
                return false, "No path could be found to the target part"
            end

            local waypoints = path:GetWaypoints()
            showWaypointDebugMarkers(waypoints)
            local blockedWaypointIndex = nil
            local currentWaypointIndex = 2
            local blockedConnection = path.Blocked:Connect(function(waypointIndex)
                if waypointIndex >= currentWaypointIndex then
                    blockedWaypointIndex = waypointIndex
                end
            end)

            local shouldRecompute = false
            local movementError = nil
            for index = 2, #waypoints do
                currentWaypointIndex = index
                if blockedWaypointIndex and blockedWaypointIndex >= index then
                    shouldRecompute = true
                    break
                end

                local waypoint = waypoints[index]
                if waypoint.Action == Enum.PathWaypointAction.Jump then
                    humanoid:ChangeState(Enum.HumanoidStateType.Jumping)
                end

                humanoid:MoveTo(waypoint.Position)
                local reached = humanoid.MoveToFinished:Wait()

                if blockedWaypointIndex and blockedWaypointIndex >= index then
                    shouldRecompute = true
                    break
                elseif not reached then
                    movementError = "NPC failed to reach path waypoint " .. tostring(index)
                    break
                end
            end

            blockedConnection:Disconnect()
            if movementError then
                return false, movementError
            end
            if not shouldRecompute then
                clearWaypointDebugMarkers()
                return true
            end
        end

        return false, "Path remained blocked after 3 attempts"
    end

    humanoid:MoveTo(target.Position)
    local reached = humanoid.MoveToFinished:Wait()
    if not reached then
        return false, "NPC failed to reach the target part"
    end
    clearWaypointDebugMarkers()
    return true
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
    local raycastParams = RaycastParams.new()
    raycastParams.FilterType = Enum.RaycastFilterType.Exclude
    raycastParams.FilterDescendantsInstances = { character }

    local followWaypoints: {PathWaypoint}? = nil
    local followWaypointIndex = 2
    local followPathBlocked = false
    local followPathConnection: RBXScriptConnection? = nil
    local followPathTarget: Vector3? = nil
    local nextPathAttemptAt = 0
    local lastRaycastAt = 0
    local isPathMoving = false
    local commandedWaypointIndex: number? = nil
    local lastWaypointProgressAt = os.clock()

    local function clearFollowPath()
        if followPathConnection then
            followPathConnection:Disconnect()
            followPathConnection = nil
        end
        followWaypoints = nil
        followPathTarget = nil
        followPathBlocked = false
        isPathMoving = false
        commandedWaypointIndex = nil
        clearWaypointDebugMarkers()
    end

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
            raycastParams.FilterDescendantsInstances = { character, playerCharacter }
            local distance = (playerRoot.Position - npcRoot.Position).Magnitude
            if distance <= config.stop_distance_studs then
                local wasMoving = isMoving or isPathMoving
                clearFollowPath()
                if wasMoving then
                    humanoid:MoveTo(npcRoot.Position)
                    isMoving = false
                end
            else
                local now = os.clock()
                local obstruction = nil
                if now - lastRaycastAt >= 0.25 then
                    obstruction = workspace:Raycast(
                        npcRoot.Position,
                        playerRoot.Position - npcRoot.Position,
                        raycastParams
                    )
                    lastRaycastAt = now
                elseif followWaypoints then
                    obstruction = true
                end

                if obstruction and config.path_find_to_go then
                    local targetMoved = not followPathTarget
                        or (playerRoot.Position - followPathTarget).Magnitude >= 4
                    local waypointStalled = isPathMoving and now - lastWaypointProgressAt >= 5
                    if (not followWaypoints or followPathBlocked or targetMoved or waypointStalled)
                        and now >= nextPathAttemptAt then
                        if followPathConnection then
                            followPathConnection:Disconnect()
                            followPathConnection = nil
                        end
                        followWaypoints = nil
                        followPathBlocked = false
                        isPathMoving = false
                        commandedWaypointIndex = nil
                        nextPathAttemptAt = now + 1
                        followPathTarget = playerRoot.Position

                        local path
                        local computed, computeError = pcall(function()
                            path = pfs:CreatePath({
                                AgentCanClimb = true,
                                Costs = {
                                    SAFE_GROUND = 0,
                                },
                            })
                            path:ComputeAsync(npcRoot.Position, playerRoot.Position)
                        end)

                        if not following or followToken ~= runToken then
                            break
                        end

                        if computed and path.Status == Enum.PathStatus.Success then
                            followWaypoints = path:GetWaypoints()
                            followWaypointIndex = 2
                            lastWaypointProgressAt = now
                            showWaypointDebugMarkers(followWaypoints)
                            followPathConnection = path.Blocked:Connect(function(blockedIndex)
                                if blockedIndex >= followWaypointIndex then
                                    followPathBlocked = true
                                end
                            end)
                        elseif not computed then
                            warn("[char_handler]: failed to compute follow path: " .. tostring(computeError))
                        else
                            warn("[char_handler]: no path found while following player")
                        end
                    end

                    local waypoints = followWaypoints
                    if waypoints and #waypoints >= 2 then
                        local waypoint = waypoints[followWaypointIndex]
                        if waypoint and (npcRoot.Position - waypoint.Position).Magnitude <= 2.5 then
                            followWaypointIndex += 1
                            commandedWaypointIndex = nil
                            lastWaypointProgressAt = now
                            waypoint = waypoints[followWaypointIndex]
                        end

                        if waypoint then
                            if commandedWaypointIndex ~= followWaypointIndex then
                                if waypoint.Action == Enum.PathWaypointAction.Jump then
                                    humanoid:ChangeState(Enum.HumanoidStateType.Jumping)
                                end
                                humanoid:MoveTo(waypoint.Position)
                                commandedWaypointIndex = followWaypointIndex
                            end
                            isPathMoving = true
                            isMoving = false
                        else
                            clearFollowPath()
                        end
                    elseif waypoints then
                        clearFollowPath()
                    end

                    if not followWaypoints and (not isMoving or now - lastMoveAt >= 0.5) then
                        humanoid:MoveTo(playerRoot.Position)
                        lastMoveAt = now
                        isMoving = true
                    end
                else
                    if followWaypoints then
                        clearFollowPath()
                    end
                    if not isMoving or now - lastMoveAt >= 0.5 then
                        humanoid:MoveTo(playerRoot.Position)
                        lastMoveAt = now
                        isMoving = true
                    end
                end
            end
        elseif isMoving or isPathMoving then
            clearFollowPath()
            humanoid:MoveTo(npcRoot.Position)
            isMoving = false
        end

        rs.Heartbeat:Wait()
    end

    clearFollowPath()
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