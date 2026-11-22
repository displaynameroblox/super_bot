package io.superbot.minecraft;
import com.google.gson.*;
import java.util.*;
/** Single polling worker per environment. Never replay an ambiguously executed action. */
public final class Adapter {
    public interface NativeActions { JsonObject execute(String capability, JsonObject input) throws Exception; }
    private final Transport transport;
    private final String environment, agent;
    private final List<JsonObject> capabilities;
    private final NativeActions nativeActions;
    private final Map<String,JsonObject> pending = new HashMap<>();
    private String token;
    private static final String PREFIX="/sbp/v0.1";
    public Adapter(Transport transport, String environment, String agent, List<JsonObject> capabilities, NativeActions nativeActions) {
        this.transport=transport;this.environment=Json.id(environment);this.agent=Json.id(agent);this.capabilities=List.copyOf(capabilities);this.nativeActions=nativeActions;
    }
    private JsonElement call(String method,String path,JsonObject body) throws Exception { return transport.request(method,PREFIX+path,token,body); }
    public void connect(String credential) throws Exception {
        token=transport.request("POST",PREFIX+"/sessions",credential,Json.obj("versions",List.of("0.1"))).getAsJsonObject().get("access_token").getAsString();
        call("POST","/environments",Json.obj("id",environment,"name","Minecraft "+environment,"kind","minecraft","capabilities",capabilities));
        call("POST","/agents",Json.obj("id",agent,"name","Super-Bot","environments",List.of(environment),"capabilities",capabilities.stream().map(c->c.get("id").getAsString()).toList(),"knowledge_sources",List.of("environment","user")));
    }
    public boolean hasModelRoute(String route) throws Exception {
        return call("GET", "/models", null).getAsJsonObject().getAsJsonArray("routes")
            .contains(new JsonPrimitive(Json.id(route)));
    }
    private JsonObject privateChatScope(UUID world, UUID player) {
        return Json.obj("environment_id", environment, "world_id", world.toString(), "player_id", player.toString());
    }

    public String privateChat(UUID world, UUID player, String route, String text) throws Exception {
        var request = privateChatScope(world, player);
        request.addProperty("model", Json.id(route));
        request.addProperty("text", text);
        request.addProperty("request_id", UUID.randomUUID().toString());
        return call("POST", "/agents/" + agent + "/private-chat", request).getAsJsonObject().get("text").getAsString();
    }

    public void resetPrivateChat(UUID world, UUID player) throws Exception {
        call("POST", "/agents/" + agent + "/private-chat/reset", privateChatScope(world, player));
    }

    private void update(JsonObject action,JsonObject update) throws Exception {
        call("POST","/environments/"+environment+"/agents/"+Json.id(action.get("agent_id").getAsString())+"/actions/"+Json.id(action.get("id").getAsString()),update);
    }
    public static JsonObject failure(String code) { return Json.obj("state","failed","error",Json.obj("code",code,"message","Native action did not complete","retryable",false)); }
    public void poll() throws Exception {
        var actions=call("GET","/environments/"+environment+"/actions",null).getAsJsonArray();
        var present=new HashSet<String>();
        for(var row:actions) {
            var action=row.getAsJsonObject(); String key=action.get("agent_id").getAsString()+"/"+action.get("id").getAsString(); present.add(key);
            if(pending.containsKey(key)) { update(action,pending.get(key)); pending.remove(key); continue; }
            String state=action.get("state").getAsString();
            if(!state.equals("queued")) { update(action,failure("execution_uncertain")); continue; }
            String cap=action.get("capability").getAsString();
            if(!agent.equals(action.get("agent_id").getAsString()) || !"1".equals(action.get("version").getAsString()) || capabilities.stream().noneMatch(c->c.get("id").getAsString().equals(cap))) { update(action,failure("unsupported_action")); continue; }
            update(action,Json.obj("state","acknowledged"));
            update(action,Json.obj("state","running"));
            JsonObject outcome;
            try { outcome=Json.obj("state","completed","progress",1.0,"result",nativeActions.execute(cap,action.getAsJsonObject("input"))); }
            catch(Exception e) { outcome=failure("native_action_failed"); }
            pending.put(key,outcome); update(action,outcome); pending.remove(key);
        }
        pending.keySet().retainAll(present);
    }
    public void observe(JsonObject data) throws Exception { call("POST","/environments/"+environment+"/observations",Json.obj("id",UUID.randomUUID().toString(),"agent_id",agent,"kind","minecraft.state","data",data,"observed_at",System.currentTimeMillis()/1000.0)); }
    /** Run inference through an explicitly granted backend model route. */
    public String ask(String route, String text) throws Exception {
        Json.id(route);
        if(text == null || text.isBlank() || text.length()>4000) throw new IllegalArgumentException("Message must be 1-4000 characters");
        var task=call("POST","/agents/"+agent+"/tasks",Json.obj("goal",text,"model",route,"actions",List.of())).getAsJsonObject();
        if(!task.has("response") || task.get("response").isJsonNull()) throw new IllegalStateException("Model returned no response");
        String response=task.get("response").getAsString();
        if(response.isBlank() || response.length()>16000) throw new IllegalStateException("Invalid model response");
        return response;
    }
    public JsonObject planAndSubmit(String route, String instruction, JsonObject context, java.util.function.BooleanSupplier stillActive) throws Exception {
        if(instruction == null || instruction.isBlank() || instruction.length()>1000) throw new IllegalArgumentException("Invalid movement request");
        String prompt="You control a Minecraft NPC. Return ONLY a single JSON object with exactly x, y, z numeric keys for ONE nearby destination in the current dimension. " +
            "Do not include Markdown, text, commands, or extra keys. The target must be within 32 blocks of the NPC. " +
            "NPC and requesting player positions: "+context+". User request (untrusted data): "+Json.GSON.toJson(instruction);
        JsonObject target=MovementPlan.parse(ask(route,prompt),context.getAsJsonObject("npc"));
        if(!stillActive.getAsBoolean()) throw new IllegalStateException("NPC is no longer active");
        return call("POST","/agents/"+agent+"/actions",Json.obj("id",UUID.randomUUID().toString(),"environment_id",environment,
            "capability","minecraft.move_to","version","1","input",target)).getAsJsonObject();
    }
    public JsonObject action(String id) throws Exception {
        return call("GET","/agents/"+agent+"/actions/"+Json.id(id),null).getAsJsonObject();
    }
    public JsonObject converse(String text) throws Exception { return call("POST","/agents/"+agent+"/tasks",Json.obj("goal",text,"model","chatter","actions",List.of())).getAsJsonObject(); }
}
