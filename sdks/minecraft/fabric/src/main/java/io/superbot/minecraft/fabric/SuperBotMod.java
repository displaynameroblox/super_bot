package io.superbot.minecraft.fabric;

import io.superbot.minecraft.*;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.google.gson.JsonObject;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.npc.Villager;
import net.minecraft.world.level.storage.LevelResource;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.LoggerFactory;

/** One temporary villager NPC in the integrated server; never installs on a remote server. */
public final class SuperBotMod implements ModInitializer {
    private Villager npc;
    private ScheduledExecutorService worker;
    private volatile boolean connected;
    private volatile String connectionStatus = "SBP disconnected; check environment settings";
    private volatile boolean modelRouteAvailable = true;
    private volatile Adapter activeAdapter;
    private volatile UUID privateChatWorld;
    private final AtomicBoolean modelBusy = new AtomicBoolean();
    private volatile long generation;
    public void onInitialize() {
        CommandRegistrationCallback.EVENT.register((dispatcher, access, selection) -> dispatcher.register(
            Commands.literal("superbot").requires(s -> s.hasPermission(2) && !s.getServer().isDedicatedServer())
                .then(Commands.literal("spawn").executes(ctx -> {
                    if(npc!=null && npc.isAlive()) return 0;
                    var player=ctx.getSource().getPlayerOrException();
                    npc=new Villager(EntityType.VILLAGER,player.serverLevel()) {
                        @Override protected void customServerAiStep() { /* SDK owns navigation; disable villager brain. */ }
                    };
                    npc.moveTo(player.getX()+1,player.getY(),player.getZ(),0,0);
                    npc.setCustomName(Component.literal("Super-Bot")); npc.setCustomNameVisible(true);
                    npc.setPersistenceRequired(); npc.setInvulnerable(true);
                    player.serverLevel().addFreshEntity(npc);
                    start(ctx.getSource().getServer());
                    ctx.getSource().sendSuccess(() -> Component.literal("Super-Bot spawned. Backend status: /superbot status"),false);
                    return 1;
                }))
                .then(Commands.literal("status").executes(ctx -> { ctx.getSource().sendSuccess(() -> Component.literal(connectionStatus),false); return 1; }))
                .then(Commands.literal("remove").executes(ctx -> { stop(); return 1; }))
                .then(Commands.literal("session").then(Commands.literal("new").executes(ctx -> {
                    resetChat(ctx.getSource().getServer(), ctx.getSource().getPlayerOrException());
                    return 1;
                })))
                .then(Commands.literal("chat").then(Commands.argument("message",StringArgumentType.greedyString()).executes(ctx -> {
                    var player=ctx.getSource().getPlayerOrException();
                    requestModel(ctx.getSource().getServer(), player, StringArgumentType.getString(ctx,"message"), false);
                    return 1;
                })))
                .then(Commands.literal("move").then(Commands.argument("request",StringArgumentType.greedyString()).executes(ctx -> {
                    var player=ctx.getSource().getPlayerOrException();
                    requestModel(ctx.getSource().getServer(), player, StringArgumentType.getString(ctx,"request"), true);
                    return 1;
                })))));
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> stop());
    }
    private void start(MinecraftServer server) {
        if(worker!=null) worker.shutdownNow();
        long current=++generation;
        connectionStatus = "SBP disconnected; check environment settings";
        privateChatWorld = null;
        Path worldIdentity = server.getWorldPath(LevelResource.ROOT).resolve("superbot-world-id");
        String credential=System.getenv("SBP_ADAPTER_TOKEN");
        if(credential==null || credential.isBlank()) return;
        worker=Executors.newSingleThreadScheduledExecutor(r -> { var t=new Thread(r,"superbot-sbp");t.setDaemon(true);return t; });
        worker.execute(() -> {
            try {
                String ca=System.getenv("SBP_CA");
                var transport = new HttpsTransport(System.getenv().getOrDefault("SBP_URL","https://127.0.0.1:8767"),ca==null?null:Path.of(ca));
                boolean hasDescriptor = transport.verifyHost(System.getenv("SBP_NODE_ID"));
                var adapter=new Adapter(transport,
                    System.getenv().getOrDefault("SBP_ENVIRONMENT","minecraft-npc"),System.getenv().getOrDefault("SBP_AGENT","minecraft-npc"),Capabilities.npc(),
                    (cap,input) -> onServer(server,() -> { if(generation!=current) throw new IllegalStateException("NPC replaced"); return execute(cap,input); }));
                adapter.connect(credential);
                boolean routeAvailable = !hasDescriptor || adapter.hasModelRoute(modelRoute());
                UUID chatWorld = transport.supportsPersistentPrivateChat() ? worldId(worldIdentity) : null;
                if(generation!=current) return;
                modelRouteAvailable = routeAvailable;
                privateChatWorld = chatWorld;
                connectionStatus = !routeAvailable ? "SBP connected; configured model route is unavailable or not granted"
                    : chatWorld != null ? "SBP connected; private chat history enabled" : "SBP connected";
                activeAdapter=adapter; connected=true;
                while(generation==current && !Thread.currentThread().isInterrupted()) {
                    adapter.poll(); adapter.observe(onServer(server,() -> { if(generation!=current) throw new IllegalStateException("NPC replaced"); return execute("minecraft.observe",Json.obj()); }));
                    Thread.sleep(2000);
                }
            } catch(InterruptedException e) { Thread.currentThread().interrupt(); }
            catch(Exception e) {
                if (generation == current) connectionStatus = "SBP disconnected; check the configured host, trust and adapter grants";
                LoggerFactory.getLogger("superbot").warn("SBP stopped ({}). Remove and spawn to reconnect.",e.getClass().getSimpleName());
            }
            finally { if(generation==current) { connected=false; activeAdapter=null; } }
        });
    }
    private static UUID worldId(Path file) throws IOException {
        if (!Files.exists(file)) {
            var temporary = Files.createTempFile(file.getParent(), "superbot-world-", ".tmp");
            try {
                Files.writeString(temporary, UUID.randomUUID().toString());
                Files.move(temporary, file, java.nio.file.StandardCopyOption.ATOMIC_MOVE);
            } finally {
                Files.deleteIfExists(temporary);
            }
        }
        return UUID.fromString(Files.readString(file).strip());
    }

    private void resetChat(MinecraftServer server, net.minecraft.server.level.ServerPlayer player) {
        Adapter adapter = activeAdapter;
        UUID world = privateChatWorld;
        if (!connected || adapter == null || world == null) {
            player.sendSystemMessage(Component.literal("Persistent private chat is unavailable on this SBP connection."));
            return;
        }
        if (!modelBusy.compareAndSet(false, true)) {
            player.sendSystemMessage(Component.literal("Wait for the current Super-Bot request to finish."));
            return;
        }
        long current = generation;
        UUID playerId = player.getUUID();
        Thread.ofVirtual().start(() -> {
            String notice;
            try {
                adapter.resetPrivateChat(world, playerId);
                notice = "Private conversation cleared.";
            } catch (Exception error) {
                notice = "Could not clear the private conversation.";
            }
            String output = notice;
            server.execute(() -> {
                modelBusy.set(false);
                if (generation == current && server.getPlayerList().getPlayer(playerId) == player) {
                    player.sendSystemMessage(Component.literal(output));
                }
            });
        });
    }

    private static String modelRoute() {
        return System.getenv().getOrDefault("SBP_MODEL_ROUTE", "existing");
    }
    private void requestModel(MinecraftServer server, net.minecraft.server.level.ServerPlayer player, String message, boolean movement) {
        Adapter adapter=activeAdapter;
        if(!connected || adapter==null || npc==null || !npc.isAlive()) { player.sendSystemMessage(Component.literal("Super-Bot is not connected.")); return; }
        if(!modelRouteAvailable) { player.sendSystemMessage(Component.literal("The configured SBP model route is unavailable or not granted.")); return; }
        if(movement && npc.level()!=player.level()) { player.sendSystemMessage(Component.literal("You and Super-Bot must be in the same dimension.")); return; }
        if(message.length()>1000) { player.sendSystemMessage(Component.literal("Request is too long (maximum 1000 characters).")); return; }
        if(!modelBusy.compareAndSet(false,true)) { player.sendSystemMessage(Component.literal("Super-Bot is already thinking.")); return; }
        long current=generation;
        var context=Json.obj("npc",Json.obj("x",npc.getX(),"y",npc.getY(),"z",npc.getZ()),
            "player",Json.obj("x",player.getX(),"y",player.getY(),"z",player.getZ()),"dimension",npc.level().dimension().location().toString());
        var playerId=player.getUUID();
        UUID chatWorld = privateChatWorld;
        String route=modelRoute();
        player.sendSystemMessage(Component.literal("Super-Bot is thinking..."));
        Thread thread=new Thread(() -> {
            String result;
            try {
                if(movement) {
                    var action=adapter.planAndSubmit(route,message,context,() -> generation==current && connected);
                    String actionId=action.get("id").getAsString();
                    String state=action.get("state").getAsString();
                    for(int i=0;i<30 && generation==current && connected && !java.util.Set.of("completed","failed","cancelled").contains(state);i++) {
                        Thread.sleep(1000);
                        action=adapter.action(actionId); state=action.get("state").getAsString();
                    }
                    result=switch(state) {
                        case "completed" -> "Navigation started. I may not reach the destination.";
                        case "failed", "cancelled" -> "Movement could not start ("+state+").";
                        default -> "Movement is still pending (action "+actionId+").";
                    };
                } else if (chatWorld != null) {
                    result = adapter.privateChat(chatWorld, playerId, route, message);
                } else {
                    result=adapter.ask(route,"Reply conversationally as Super-Bot in Minecraft. Keep it short. Player says: "+Json.GSON.toJson(message));
                }
            } catch(Exception e) {
                LoggerFactory.getLogger("superbot").warn("Model request failed: {}", e.toString());
                result="Could not complete the model request. Check the backend model route and game log.";
            }
            String output=result.length()>1000?result.substring(0,1000):result;
            server.execute(() -> {
                modelBusy.set(false);
                if (generation == current && server.getPlayerList().getPlayer(playerId) == player) {
                    player.sendSystemMessage(Component.literal("Super-Bot: " + output));
                }
            });
        },"superbot-model");
        thread.setDaemon(true); thread.start();
    }
    private <T> T onServer(MinecraftServer server,Callable<T> task) throws Exception {
        var future=new CompletableFuture<T>(); var expired=new AtomicBoolean();
        server.execute(() -> { if(expired.get()) return; try { future.complete(task.call()); } catch(Exception e) { future.completeExceptionally(e); } });
        try { return future.get(5,TimeUnit.SECONDS); } finally { expired.set(true); }
    }
    private JsonObject execute(String capability,JsonObject input) {
        if(npc==null || !npc.isAlive()) throw new IllegalStateException("NPC unavailable");
        return switch(capability) {
            case "minecraft.observe" -> Json.obj("x",npc.getX(),"y",npc.getY(),"z",npc.getZ(),"health",npc.getHealth(),"dimension",npc.level().dimension().location().toString(),"navigation_done",npc.getNavigation().isDone());
            case "minecraft.stop" -> { npc.getNavigation().stop(); yield Json.obj("stopped",true); }
            case "minecraft.move_to" -> {
                double x=input.get("x").getAsDouble(),y=input.get("y").getAsDouble(),z=input.get("z").getAsDouble();
                if(!Double.isFinite(x)||!Double.isFinite(y)||!Double.isFinite(z)||npc.distanceToSqr(x,y,z)>32*32) throw new IllegalArgumentException("Target must be within 32 blocks");
                boolean accepted=npc.getNavigation().moveTo(x,y,z,0.6);
                if(!accepted) throw new IllegalStateException("No path");
                yield Json.obj("navigation_started",true,"arrived",false);
            }
            default -> throw new IllegalArgumentException("Unsupported capability");
        };
    }
    private void stop() { generation++; connected=false; activeAdapter=null; connectionStatus="SBP disconnected; spawn an NPC to connect"; if(worker!=null) {worker.shutdownNow();worker=null;} if(npc!=null) {npc.discard();npc=null;} }
}
