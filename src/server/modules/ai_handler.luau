local EncodingService = game:GetService("EncodingService")
local HttpService = game:GetService("HttpService")
local self = {}

local data_link = game.ReplicatedStorage.Folder.data_link
local running_port = "8080"
local api_key = "fuck you tunsi"
local url

function self:start_up()
    if HttpService.HttpEnabled == false then
        error("Can't reach local server, HttpService.HttpEnabled is set to false")
    end
    local url = "http://localhost:"..running_port""
    local pinged, reuslt = HttpService.GetAsync(url"/health")
    if not pinged then
        warn("failed to ping "..url.."/health! \n ".. tostring(reuslt) .."")
        return
    elseif (pinged and reuslt.StatusCode == 200) then
        print("server return a htpp stautsecode of 200, got message ".. tostring(reuslt.body) .."!")
        url = "http://localhost:"..running_port
    else
        error("unexpected error has occred!")
    end
    return true -- nothing yet
end

function start_ses(player : Player, sys_inter : string, chat_log : string,password : string, session_id : number) : (boolean,string?)
    if not player then
        return false, "SERVER did not pass a player!"
    end
    if not sys_inter then
        sys_inter = "You are a close friend to this player, you like to talk to him a lot and spend time together! you may role play with text with *, i.e *i went to look for ...*, you may use mark down to style your text!"
        warn("Server did not pass a sys_inter string! defaulting to normal sys_inter string!")
    end
    if not chat_log then
        chat_log = "SYSTEM: user just start a new sesion with no chat log, you may start with your first message beased on your persona!"
        warn("server did not pass a chat_log, starting a new one")
    end
    if not session_id then
        warn("no session Id given, letting AI server making one")
    end
    if not password then
        warn("no password given, this might be riskly if you are in a public network!!") --this was a self-healing project soo... :/ I WANT SOMEONE PLEASE!!!
    end
    local before_json_ed = {plr_name= player.DisplayName,chat_log=chat_log,AI_persona=sys_inter,session_id=session_id,password=password}
    local json = HttpService.JSONEncode(before_json_ed)
    
    local posted, body = pcall(HttpService.RequestAsync({
         Url = url.."/start_ses",
        Method = "POST",
        Headers = {["Content-Type"] = "application/json"},
        body = json
    }))
    if not posted then
        warn("fail to send to server!")
        return false, tostring(body)
    end
    if body.Success then
        print("started session! got stauts code:".. body.StatusCode .." with message: \n ".. body.body .." ")
        return body.body
    else
        print("failed to start sesion! got status code from server ".. body.StatusCode .."! ")
    end

    return true
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