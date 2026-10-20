--!nocheck
local cs = game:GetService("Chat")
local self = {}

local mem = {}
--mem.char : model = nil 

function self:start_up()
    if not mem.char then
        print("getting char...")
        local char_handler = require(script.Parent.char_handler)
        local module = require(char_handler)
        local char_get, char, char_get_error = module.getchar()
        if not char_get then
            error("failed to start due not getting char: ".. char_get_error  .." ")
            return false
        else
            mem.char = char
            print("got char")
        end
        local head = mem.char:FindFirstChild("Head")
        if not head then
            warn("he cant talk :<")
            return false
        end
        local s,r = pcall(function(...)
            cs:Chat(head,"meow meow meow")
        end)
        if not s then
            warn("failed to test chating for char! dose he have a head? ".. tostring(r) .." ")
            return false
        elseif s == true then
            print("Check finished")
        end
    end
    print("start up finished")
    return true
end

function self.say(msg : string, char : Model)
    if not char then
        return false, "no model given! did you forget to pass one?"
    end
    if not msg or typeof(msg) ~= "string" then
        return false, "invaild string given or not found! did you pass a string?"
    end
    local head = char:FindFirstChild("Head")
    if not head then
        return false, "cant find head for model! did you check there is one?"
    end
    local said, say_error = pcall(function()
        cs:Chat(head, tostring(msg))
    end)
    if not said then
        return false, "Failed to make char to say msg, \n ".. tostring(say_error) .." "
    else
        return true
    end
end


return self