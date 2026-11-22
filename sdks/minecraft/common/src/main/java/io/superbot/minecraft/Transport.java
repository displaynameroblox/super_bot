package io.superbot.minecraft;
import com.google.gson.*;
@FunctionalInterface
public interface Transport { JsonElement request(String method, String path, String token, JsonObject body) throws Exception; }
