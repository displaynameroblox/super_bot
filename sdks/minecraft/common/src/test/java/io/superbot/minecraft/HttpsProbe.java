package io.superbot.minecraft;
import java.nio.file.Path;
import java.util.List;
/** Run by tools/https_smoke.py against the real Python host. */
public final class HttpsProbe {
    public static void main(String[] args) throws Exception {
        var transport=new HttpsTransport(args[0],Path.of(args[1]));
        transport.verifyHost(args[2]);
        try {
            transport.verifyHost("nod_00000000-0000-4000-8000-000000000000");
            throw new AssertionError("Unexpected node ID accepted");
        } catch (java.io.IOException expected) {
            if (!"SBP node identity mismatch".equals(expected.getMessage())) throw expected;
        }
        transport.verifyHost(args[2]);
        String credential=System.getenv("SBP_ADAPTER_TOKEN");
        var adapter=new Adapter(transport,"world","bot",Capabilities.npc(),(cap,input)->Json.obj("x",12));
        adapter.connect(credential);
        if (!adapter.hasModelRoute("existing") || adapter.hasModelRoute("missing")) {
            throw new AssertionError("Model route discovery mismatch");
        }
        String token=transport.request("POST","/sbp/v0.1/sessions",credential,Json.obj("versions",List.of("0.1"))).getAsJsonObject().get("access_token").getAsString();
        transport.request("POST","/sbp/v0.1/agents/bot/actions",token,Json.obj("id","https-test","environment_id","world","capability","minecraft.observe","version","1","input",Json.obj()));
        adapter.poll(); adapter.observe(Json.obj("x",12));
        var result=transport.request("GET","/sbp/v0.1/agents/bot/actions/https-test",token,null).getAsJsonObject();
        if(!result.get("state").getAsString().equals("completed") || result.getAsJsonObject("result").get("x").getAsInt()!=12) throw new AssertionError(result);
        if(!adapter.ask("existing","Hello").equals("Hello from the model")) throw new AssertionError("Chat response mismatch");
        if (!transport.supportsPersistentPrivateChat()) throw new AssertionError("Private chat feature missing");
        var world = java.util.UUID.randomUUID();
        var player = java.util.UUID.randomUUID();
        adapter.privateChat(world, player, "existing", "Remember Alex");
        if (!adapter.privateChat(world, player, "existing", "What is my name?").equals("Alex"))
            throw new AssertionError("Private chat history missing");
        if (!adapter.privateChat(world, java.util.UUID.randomUUID(), "existing", "What is my name?").equals("Unknown"))
            throw new AssertionError("Private chat crossed player boundary");
        adapter.resetPrivateChat(world, player);
        if (!adapter.privateChat(world, player, "existing", "What is my name?").equals("Unknown"))
            throw new AssertionError("Private chat reset failed");
        var context=Json.obj("npc",Json.obj("x",0,"y",64,"z",0),"player",Json.obj("x",3,"y",64,"z",0));
        var movement=adapter.planAndSubmit("existing","come here",context,()->true);
        adapter.poll();
        var completed=adapter.action(movement.get("id").getAsString());
        if(!completed.get("state").getAsString().equals("completed")) throw new AssertionError(completed);
        System.out.println("Java-to-Python HTTPS action, observation, model chat and planned movement passed");
    }
}
