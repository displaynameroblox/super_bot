--!nocheck
local self = {}

local mem = {}

function self.makechar() : boolean
    local s,r = pcall(function()
        local model : Instance = game.ReplicatedStorage.char.feras_1256
        return model
    end)
    if not s then
        return false, "Failed to clone avatar due: ".. tostring(r).."\n did you forget to add one?"
    end
    if s then
        s.Position = Vector3(0,10,0)
        mem.char = s
        return true
    end
end

function self.getchar() : (boolean,Instance)
    if not mem.char then
        local count = 0
        repeat
            if count > 10 then
                error("failed to get char, run makechar()")
                break
            end
            self.makechar()
            count += 1
        until mem.char
        return false
    else
        return true, mem.char
    end
end

function self.movechar(vec3 : Vector3, ex : {overdrive_char : boolean, char : Model})
    warn("[Char_handler]: as of 17 oct 26, there is no path finding! will be added later!")
    local char = nil -- to be added later!
    char = mem.char
    local humanoid : Humanoid = char:FindFirstChild("Humanoid")
    if not humanoid then
        return false, "Cannot find humanoid, did you give a invaild char?"
    end
    if not vec3 then
        return false, "No vec3 given or not found!, did you forget to pass one?"
    end
    warn("[Char_handler]: As of 17 oct 26, there is not calating for vec3.")
    humanoid:MoveTo(vec3)
end


return self