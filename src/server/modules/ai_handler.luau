local self = {}

local data_link = game.ReplicatedStorage.Folder.data_link

function self:start_up()
    local api_key = "soon"
    data_link:FireAllClients("!##STARTED_SERVER A=".. tostring(api_key) .."") -- this system will be replaced!
    return true -- nothing yet
end

function self:test(player : Player)
    if not player then
        warn("no player was passed! the result may be inaccrnet!")
    end
    data_link:FireAllClients("##Say !M='Test from server!!'")
    task.wait(5)
    data_link:FireAllClients("##follow_main_player")
end



return self