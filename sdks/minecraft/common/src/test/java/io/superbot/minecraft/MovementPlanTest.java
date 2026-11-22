package io.superbot.minecraft;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class MovementPlanTest {
    private final com.google.gson.JsonObject origin=Json.obj("x",0,"y",64,"z",0);
    @Test void acceptsNearbyNumericTarget() { assertEquals(3,MovementPlan.parse("{\"x\":3,\"y\":64,\"z\":0}",origin).get("x").getAsInt()); }
    @Test void acceptsSingleJsonCodeFence() {
        assertEquals(3, MovementPlan.parse("```json\n{\"x\":3,\"y\":64,\"z\":0}\n```",origin).get("x").getAsInt());
    }
    @Test void rejectsInvalidOrDangerousPlans() {
        for(String plan : new String[]{"hello", "{\"x\":100,\"y\":64,\"z\":0}", "{\"x\":0,\"y\":64,\"z\":0,\"command\":\"kill\"}", "{\"x\":\"1\",\"y\":64,\"z\":0}", "{\"x\":0,\"y\":1000,\"z\":0}"})
            assertThrows(IllegalArgumentException.class,()->MovementPlan.parse(plan,origin),plan);
    }
}
