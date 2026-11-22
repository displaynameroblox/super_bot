local WorldObserver = {}


function WorldObserver.init_obersr()
    local obrser = {}
    obrser.__index = obrser

    function obrser:init(
        Char : Model,
        seettings : {
            pov : "inperson" | "GOD", -- god?
            knowange_setting : 0 | 1, -- bc if we added more stuff later we can set it up here
            
        }
    )
        
    end

end

return WorldObserver
