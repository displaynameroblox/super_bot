local self = {}

local _config = {
    _self_test_on_start_up = nil
}

function self.test()
    task.wait(1)
    local api = require(script.Parent:WaitForChild("api_handler"))
    local char_handler = require(script.Parent.char_handler)
    local chat_handler = require(script.Parent:WaitForChild("chat_handler"))
    local players = game:GetService("Players")

    local apiSmokeOk, apiSmokeError = api:cmd("##Say !M='API smoke test from client handler'")
    if not apiSmokeOk then
        warn("API smoke test failed before gameplay: " .. tostring(apiSmokeError))
        return false, apiSmokeError
    end

    print("making char..")
    local made, model, make_error = char_handler.getchar()
    if not made or not model then
        local message = make_error or "No character model was returned"
        warn("Failed to make char: \n " .. message)
        return false, message
    end

    local said, say_error = chat_handler.say("I am starting the moving test, i should be moving any secend now...",model)
    if not said then
        warn(say_error)
        return false, say_error
    end

    local targetpart = Instance.new("Part")
    targetpart.Anchored = true
    targetpart.CanCollide = true
    targetpart.Transparency = 0
    targetpart.Position = Vector3.new(-60.104, 0.5, 20.809)
    targetpart.Parent = workspace

    local moved, moveerror = char_handler.GoToPart(targetpart)
    targetpart:Destroy()
    if not moved then
        chat_handler.say("i cannot go there bc a inside error: \n " .. tostring(moveerror), model)
        return false, moveerror
    end

    task.wait(10)
    chat_handler.say("ima follow u ;3", model)
    local followResult, followError
    task.spawn(function()
        followResult, followError = char_handler.start_follow(players.LocalPlayer)
    end)
    task.wait(40)
    char_handler.stop_follow()
    task.wait()
    if followResult ~= true then
        local message = followError or "The direct follow test did not complete successfully"
        warn(message)
        return false, message
    end
    said, say_error = chat_handler.say("i want to test the api rq..", model)
    if not said then
        warn(say_error)
        return false, say_error
    end
    task.wait()
    local apiTestOk, apiTestError = api:sent_server_command("##Test !M='server API test'")
    if not apiTestOk then
        warn(apiTestError)
        return false, apiTestError
    end
     return true
end

function self.Start()
    local chat_handler = require(script.Parent:WaitForChild("chat_handler"))
    local started = chat_handler:start_up()
    if started == false then
        warn("chat failed to work!")
    elseif started == true then
        print("started chat handler")
    else
        error("caught an unexpected return! module did not return a vaule! got ".. tostring(typeof(started)) .." ")
    end
    print("finished")
end

return self