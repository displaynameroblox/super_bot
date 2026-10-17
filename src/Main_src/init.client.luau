--!nocheck
game:GetService("UserInputService").InputBegan(function(i,p)
    if p then return end
    if i.KeyCode == Enum.KeyCode.P then
        local module = script.Parent.modules.char_handler
        local char_handler = require(module)
        local players = game:GetService("Players")

        print("making char..")
        local made, _, makeError = char_handler.makechar()
        if not made then
                error(makeError or "Failed to create avatar")
        end

        local player = players.LocalPlayer
        local character = player.Character or player.CharacterAdded:Wait()
        local rootPart = character:WaitForChild("HumanoidRootPart")
        local moved, moveError = char_handler.movechar(rootPart.Position)
        if not moved then
            warn("[Char_handler]: " .. (moveError or "Failed to move avatar"))
        end
    end
end)