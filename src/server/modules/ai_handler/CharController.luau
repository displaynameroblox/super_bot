local char = {}

function char:init_one(name: string, id: string)
    local self = {}
    self.__index = self
    
    function self:init(name : string, id : string)
        -- modules
        local api = require(script.Parent.Parent.server_api)

        function make_char()
                local workspace = game.Workspace

                local char = Instance.new("Model")
                 char.name = name

                 local humanoid = Instance.new("Humanoid")
                 humanoid.Parent = char

                local function make(name,size,color)
                local part = Instance.new("Part")
                part.Name = name
                part.Size = size
	            part.BrickColor = color
	            part.Material = Enum.Material.SmoothPlastic
	            part.BottomSurface = Enum.SurfaceType.Smooth
	            part.TopSurface = Enum.SurfaceType.Smooth
	            part.CanCollide = true
	            part.Massless = false
	            part.Parent = char
	            return part
            end

        local torso           = make("Torso", Vector3.new(2, 2, 1), BrickColor.new("Bright Blue"))
        local head            = make("Head", Vector3.new(1.2, 1.2, 1.2), BrickColor.new("Pastel Brown"))
        local humanoidRootPart = make("HumanoidRootPart", Vector3.new(2, 2, 1), BrickColor.new("Medium stone grey"))

        local leftArm         = make("Left Arm", Vector3.new(1, 2, 1), BrickColor.new("Bright Blue"))
        local rightArm        = make("Right Arm", Vector3.new(1, 2, 1), BrickColor.new("Bright Blue"))
        local leftLeg         = make("Left Leg", Vector3.new(1, 2, 1), BrickColor.new("Bright Green"))
        local rightLeg        = make("Right Leg", Vector3.new(1, 2, 1), BrickColor.new("Bright Green"))

        humanoidRootPart.Transparency = 0.5 -- Usually invisible, left visible here for debugging
        char.PrimaryPart = humanoidRootPart

        torso.Position = Vector3.new(0, 5, 0)
        head.Position = torso.Position + Vector3.new(0, 1.6, 0)
        humanoidRootPart.Position = torso.Position

        leftArm.Position = torso.Position + Vector3.new(-1.5, 0, 0)
        rightArm.Position = torso.Position + Vector3.new(1.5, 0, 0)
        leftLeg.Position = torso.Position + Vector3.new(-0.5, -2, 0)
        rightLeg.Position = torso.Position + Vector3.new(0.5, -2, 0)
        local function attachParts(part0: Part, part1: Part, jointName: string, c0: CFrame, c1: CFrame)
	        local motor = Instance.new("Motor6D")
	        motor.Name = jointName
	        motor.Part0 = part0
	        motor.Part1 = part1
	        motor.C0 = c0
	        motor.C1 = c1
	        motor.Parent = part0
        end
        attachParts(humanoidRootPart, torso, "RootJoint", CFrame.identity, CFrame.identity)
        attachParts(torso, head, "Neck", CFrame.new(0, 1, 0), CFrame.new(0, -0.6, 0))
        attachParts(torso, leftArm, "Left Shoulder", CFrame.new(-1, 0.5, 0), CFrame.new(0.5, 0.5, 0))
        attachParts(torso, rightArm, "Right Shoulder", CFrame.new(1, 0.5, 0), CFrame.new(-0.5, 0.5, 0))
        attachParts(torso, leftLeg, "Left Hip", CFrame.new(-1, -1, 0), CFrame.new(-0.5, 1, 0))
        attachParts(torso, rightLeg, "Right Hip", CFrame.new(1, -1, 0), CFrame.new(0.5, 1, 0))
        char.Parent = workspace
    end
    if not name or not id then
        warn("no name or id given, defulting..")
        if not name then
            name = "Super bot 1"
            id = "super_bot_1"
        end
       local got, agent: (boolean, api.Agent) = api:get_agent("123", id) -- for now ig 
       if got then
            warn("there is an already an agent with same id! randomizing name...")
            warn("agent info: "..agent.id.." ".. agent.name .. " ".. agent.environment .." ".. " ".. agent.capabilities.." ".. agent.knowledge_sources .. " ".. agent.owner .. " " agent.status )
       end
       name = name .. "" .. math.random(200, 900)
       id = id .. "" .. math.random(200,900)

       local agent_req: api.AgentRequest = {
        id = id,
        name = name,
        environments = {"game"},
        capabilities = {"navigation.move_to"},
        knowledge_sources = {"environment", "experience", "developer", "user", "shared", "memory"}
       }

       local registered, self.agent_registered : (boolean, api.Agent | api.ApiError) = api:register_agent("123", agent_req) -- add token later
       if not registered and not self.agent_registered.retryable then
            error("fatal error happened, got API error with code: ".. self.agent_registered.code .." with message: ".. self.agent_registered.message .. " with status code: ".. self.agent_registered.status_code)
            
       end
    end
end
end

return char