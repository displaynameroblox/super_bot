package io.superbot.minecraft;
import com.google.gson.*;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class AdapterTest {
    static final class Fake implements Transport {
        JsonObject action=Json.obj("id","a1","agent_id","bot","environment_id","world","capability","minecraft.observe","version","1","state","queued","input",Json.obj());
        List<String> states=new ArrayList<>(); boolean failComplete;
        public JsonElement request(String method,String path,String token,JsonObject body) throws Exception {
            assertTrue(path.startsWith("/sbp/v0.1/"));
            if(path.endsWith("/sessions")) { assertEquals("bootstrap",token); return Json.obj("access_token","session"); }
            assertEquals("session",token);
            if(method.equals("GET")) { var a=new JsonArray(); if(!action.get("state").getAsString().equals("completed"))a.add(action.deepCopy()); return a; }
            if(path.contains("/actions/")) { String state=body.get("state").getAsString(); if(state.equals("completed")&&failComplete) {failComplete=false;throw new java.io.IOException();} states.add(state);action.addProperty("state",state); }
            return body;
        }
    }
    @Test void executesWithoutModelAndRetriesResultWithoutRepeatingNativeEffect() throws Exception {
        var f=new Fake(); int[] count={0};
        var adapter=new Adapter(f,"world","bot",Capabilities.player(),(c,i)->{count[0]++;return Json.obj("x",1);});
        adapter.connect("bootstrap"); f.failComplete=true;
        assertThrows(java.io.IOException.class,adapter::poll); adapter.poll(); adapter.poll();
        assertEquals(1,count[0]); assertEquals(List.of("acknowledged","running","completed"),f.states);
    }
    @Test void interruptedRunningActionIsNotReplayed() throws Exception {
        var f=new Fake();f.action.addProperty("state","running");
        var adapter=new Adapter(f,"world","bot",Capabilities.player(),(c,i)->{fail("Must not execute");return null;});
        adapter.connect("bootstrap");adapter.poll();assertEquals(List.of("failed"),f.states);
    }
    @Test void wrongAgentIsRejected() throws Exception {
        var f=new Fake();f.action.addProperty("agent_id","other");
        var adapter=new Adapter(f,"world","bot",Capabilities.player(),(c,i)->{fail();return null;});
        adapter.connect("bootstrap");adapter.poll();assertEquals(List.of("failed"),f.states);
    }
    @Test void errorsBecomeFailedResults() throws Exception {
        var f=new Fake(); var adapter=new Adapter(f,"world","bot",Capabilities.player(),(c,i)->{throw new IllegalStateException();});
        adapter.connect("bootstrap");adapter.poll();assertEquals(List.of("acknowledged","running","failed"),f.states);
    }
    @Test void rejectsUnsafeOriginsAndIds() {
        for(String url:List.of("http://localhost:8767","https://user:pass@localhost","https://localhost/path","https://localhost?token=x")) assertThrows(IllegalArgumentException.class,()->new HttpsTransport(url,null));
        assertThrows(IllegalArgumentException.class,()->Json.id("../secret"));
    }
    @Test void conversationUsesOnlyChatter() throws Exception {
        var f=new Fake();
        Transport recording=(method,path,token,body)-> {
            if(path.endsWith("/tasks")) { assertEquals("chatter",body.get("model").getAsString()); assertTrue(body.getAsJsonArray("actions").isEmpty()); }
            return f.request(method,path,token,body);
        };
        var adapter=new Adapter(recording,"world","bot",Capabilities.player(),(c,i)->Json.obj());
        adapter.connect("bootstrap");adapter.converse("Hello");
    }
    @Test void unsupportedVersionNeverExecutes() throws Exception {
        var f=new Fake();f.action.addProperty("version","2");
        var adapter=new Adapter(f,"world","bot",Capabilities.player(),(c,i)->{fail();return null;});
        adapter.connect("bootstrap");adapter.poll();assertEquals(List.of("failed"),f.states);
    }
    @Test void writesContractFixture() throws Exception {
        var dir=java.nio.file.Path.of("build/fixtures");java.nio.file.Files.createDirectories(dir);
        java.nio.file.Files.writeString(dir.resolve("capabilities.json"),Json.GSON.toJson(Capabilities.npc()));
    }
}
