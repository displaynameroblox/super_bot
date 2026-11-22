package io.superbot.minecraft;
import com.google.gson.*;
import java.util.Set;
/** Strictly accept a bounded destination, never commands or arbitrary capabilities. */
public final class MovementPlan {
    private MovementPlan() {}
    public static JsonObject parse(String response, JsonObject npc) {
        if(response == null || response.length()>1000) throw new IllegalArgumentException("Invalid plan");
        // Accept the common model convention of a single fenced JSON object.
        // All coordinates and keys are still validated below.
        String candidate=response.strip();
        if(candidate.startsWith("```") && candidate.endsWith("```")) {
            int newline=candidate.indexOf('\n');
            if(newline < 0) throw new IllegalArgumentException("Invalid code fence");
            String language=candidate.substring(3,newline).strip();
            if(!language.isEmpty() && !language.equalsIgnoreCase("json")) throw new IllegalArgumentException("Invalid code fence");
            candidate=candidate.substring(newline+1,candidate.length()-3).strip();
        }
        JsonObject target;
        try { target=JsonParser.parseString(candidate).getAsJsonObject(); }
        catch(RuntimeException e) { throw new IllegalArgumentException("Model must return a JSON destination"); }
        if(!target.keySet().equals(Set.of("x","y","z"))) throw new IllegalArgumentException("Plan must contain exactly x, y, z");
        double distance=0;
        for(String axis: Set.of("x","y","z")) {
            JsonElement item=target.get(axis);
            if(!item.isJsonPrimitive() || !item.getAsJsonPrimitive().isNumber()) throw new IllegalArgumentException("Coordinates must be numbers");
            double value=item.getAsDouble(), origin=npc.get(axis).getAsDouble();
            if(!Double.isFinite(value) || !Double.isFinite(origin)) throw new IllegalArgumentException("Coordinates must be finite");
            if(axis.equals("y") ? value < -64 || value > 319 : Math.abs(value)>29999984) throw new IllegalArgumentException("Target outside world bounds");
            distance+=(value-origin)*(value-origin);
        }
        if(distance>1024) throw new IllegalArgumentException("Target is more than 32 blocks away");
        return target;
    }
}
