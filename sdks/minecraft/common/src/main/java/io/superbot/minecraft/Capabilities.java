package io.superbot.minecraft;
import com.google.gson.*;
import java.util.*;
public final class Capabilities {
    public static JsonObject capability(String id, String properties, String required) {
        return Json.obj("id",id,"version","1","input_schema",Json.object("{\"type\":\"object\",\"additionalProperties\":false,\"properties\":"+properties+",\"required\":"+required+"}"),
            "output_schema",Json.object("{\"type\":\"object\"}"),"permissions",List.of(id),"constraints",Json.obj("timeout_seconds",30,"max_concurrent",1),"cancellation",false);
    }
    public static List<JsonObject> npc() { return List.of(
        capability("minecraft.observe","{}","[]"), capability("minecraft.stop","{}","[]"),
        capability("minecraft.move_to","{\"x\":{\"type\":\"number\",\"minimum\":-29999984,\"maximum\":29999984},\"y\":{\"type\":\"number\",\"minimum\":-64,\"maximum\":319},\"z\":{\"type\":\"number\",\"minimum\":-29999984,\"maximum\":29999984}}","[\"x\",\"y\",\"z\"]")); }
    public static List<JsonObject> player() { return List.of(capability("minecraft.observe","{}","[]")); }
}
