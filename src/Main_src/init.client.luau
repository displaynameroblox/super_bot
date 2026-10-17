--!nocheck
local module = script.Parent.modules.char_handler
local char_handler = require(module)

task.wait(1)
print("making char..")
char_handler:makechar()
task.wait(5)
char_handler.movechar(game:GetService("Players").LocalPlayer.Character.HumanoidRootPart.Position)