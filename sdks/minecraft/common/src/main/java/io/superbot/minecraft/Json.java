package io.superbot.minecraft;
import com.google.gson.*;
public final class Json {
    public static final Gson GSON = new Gson();
    private Json() {}
    public static JsonObject object(String text) { return JsonParser.parseString(text).getAsJsonObject(); }
    public static JsonObject obj(Object... pairs) {
        JsonObject result = new JsonObject();
        for (int i=0;i<pairs.length;i+=2) result.add((String)pairs[i], GSON.toJsonTree(pairs[i+1]));
        return result;
    }
    public static String id(String id) {
        if (id == null || !id.matches("[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}")) throw new IllegalArgumentException("Invalid SBP identifier");
        return id;
    }
}
