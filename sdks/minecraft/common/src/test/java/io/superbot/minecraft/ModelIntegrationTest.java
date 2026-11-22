package io.superbot.minecraft;
import com.google.gson.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class ModelIntegrationTest {
    static final class Backend implements Transport {
        String response="{\"x\":3,\"y\":64,\"z\":0}"; JsonObject action; String route;
        public JsonElement request(String method,String path,String token,JsonObject body) {
            if(path.endsWith("/sessions")) return Json.obj("access_token","session");
            if(path.endsWith("/tasks")) { route=body.get("model").getAsString(); return Json.obj("response",response); }
            if(path.endsWith("/actions")) { action=body;return body; }
            return body;
        }
    }
    static Adapter adapter(Backend backend) throws Exception {
        var adapter=new Adapter(backend,"world","bot",Capabilities.npc(),(cap,input)->Json.obj());
        adapter.connect("bootstrap"); return adapter;
    }
    @Test void chatUsesConfiguredRoute() throws Exception {
        var backend=new Backend();backend.response="Hello!";
        assertEquals("Hello!",adapter(backend).ask("existing","Hello"));
        assertEquals("existing",backend.route);
    }
    @Test void movementSubmitsOnlyValidatedDestination() throws Exception {
        var backend=new Backend();
        var context=Json.obj("npc",Json.obj("x",0,"y",64,"z",0),"player",Json.obj("x",3,"y",64,"z",0));
        adapter(backend).planAndSubmit("existing","come here",context,()->true);
        assertEquals("minecraft.move_to",backend.action.get("capability").getAsString());
        assertEquals(3,backend.action.getAsJsonObject("input").get("x").getAsInt());
    }
    @Test void badOrStalePlansNeverSubmitAnAction() throws Exception {
        var backend=new Backend(); var adapter=adapter(backend);
        var context=Json.obj("npc",Json.obj("x",0,"y",64,"z",0));
        backend.response="{\"x\":999,\"y\":64,\"z\":0}";
        assertThrows(IllegalArgumentException.class,()->adapter.planAndSubmit("existing","walk",context,()->true));
        backend.response="{\"x\":3,\"y\":64,\"z\":0}";
        assertThrows(IllegalStateException.class,()->adapter.planAndSubmit("existing","walk",context,()->false));
        assertNull(backend.action);
    }
}
