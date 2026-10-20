local server_api = require(script.Parent.server_api)
local self = {}

function self:start_server()
    local time_before = os.clock()
    --start up code
    server_api:start_up()
    --end of start up code
    local time_took = os.clock() - time_before
    print("[Main_server]: took ".. time_took .." secs to start the server!")
end

return self