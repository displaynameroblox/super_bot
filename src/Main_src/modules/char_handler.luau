--!nocheck
local self = {}

local mem = {}

function self.makechar(): (boolean, Model?, string?)
    if mem.char and mem.char.Parent then
        return true, mem.char
    end

    local success, result = pcall(function()
        local container = game:GetService('ReplicatedStorage'):WaitForChild("char")
        if not container then
            error("ReplicatedStorage.char was not found")
        end

        local template = container:WaitForChild("feras_1256")
        if not template or not template:IsA("Model") then
            error("ReplicatedStorage.char.feras_1256 must be a Model")
        end

        local character = template:Clone()
        if not character:FindFirstChildOfClass("Humanoid") then
            error("Avatar model does not contain a Humanoid")
        end

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
    if typeof(vec3) ~= "Vector3" then
        return false, "A destination Vector3 is required"
    end

    local char = mem.char
    if not char or not char.Parent then
        return false, "No character is available; call makechar() first"
    end

    local humanoid = char:FindFirstChildOfClass("Humanoid")
    if not humanoid then
        return false, "Cannot find a Humanoid in the character"
    end

    humanoid:MoveTo(vec3)
    return true
end

return self