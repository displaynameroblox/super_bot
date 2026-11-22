package io.superbot.minecraft.player;

import io.superbot.minecraft.*;
import org.geysermc.mcprotocollib.auth.GameProfile;
import org.geysermc.mcprotocollib.auth.SessionService;
import org.geysermc.mcprotocollib.protocol.MinecraftConstants;
import org.geysermc.mcprotocollib.network.Session;
import org.geysermc.mcprotocollib.network.tcp.TcpClientSession;
import org.geysermc.mcprotocollib.network.packet.Packet;
import org.geysermc.mcprotocollib.network.event.session.*;
import org.geysermc.mcprotocollib.protocol.MinecraftProtocol;
import org.geysermc.mcprotocollib.protocol.packet.ingame.clientbound.ClientboundLoginPacket;
import org.geysermc.mcprotocollib.protocol.packet.ingame.clientbound.entity.player.ClientboundPlayerPositionPacket;
import org.geysermc.mcprotocollib.protocol.packet.ingame.serverbound.level.ServerboundAcceptTeleportationPacket;
import net.kyori.adventure.text.Component;
import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.*;

/** Real protocol client: login and server-reported position only, no simulated survival physics. */
public final class Main {
    private static String required(String key) { var value=System.getenv(key); if(value==null||value.isBlank())throw new IllegalArgumentException("Set "+key);return value; }
    public static void main(String[] args) throws Exception {
        if(args.length==1 && args[0].equals("--help")) { System.out.println("Set MC_HOST, MC_USERNAME, MC_UUID, MC_ACCESS_TOKEN, SBP_ADAPTER_TOKEN, SBP_CA. Optional MC_PORT, SBP_URL, SBP_ENVIRONMENT, SBP_AGENT. --offline allows a local offline-mode test server."); return; }
        boolean offline=args.length==1 && args[0].equals("--offline");
        if(args.length>0&&!offline) throw new IllegalArgumentException("Use --help or --offline");
        String name=required("MC_USERNAME"),host=required("MC_HOST");
        if(offline && !host.equals("127.0.0.1") && !host.equals("localhost") && !host.equals("::1")) throw new IllegalArgumentException("Offline mode is restricted to local tests");
        var protocol=offline?new MinecraftProtocol(name):new MinecraftProtocol(new GameProfile(UUID.fromString(required("MC_UUID")),name),required("MC_ACCESS_TOKEN"));
        var client=new TcpClientSession(host,Integer.parseInt(System.getenv().getOrDefault("MC_PORT","25565")),protocol,null);
        client.setFlag(MinecraftConstants.SESSION_SERVICE_KEY,new SessionService());
        var ready=new CompletableFuture<Void>();
        var position=new java.util.concurrent.atomic.AtomicReference<>(Json.obj("position_known",false));
        client.addListener(new SessionAdapter() {
            @Override public void packetReceived(Session session,Packet packet) {
                if(packet instanceof ClientboundLoginPacket) ready.complete(null);
                if(packet instanceof ClientboundPlayerPositionPacket p) {
                    // Relative updates are explicitly marked; do not misrepresent them as absolute world coordinates.
                    position.set(Json.obj("position_known",true,"x",p.getX(),"y",p.getY(),"z",p.getZ(),"relative_flags",p.getRelative().toString()));
                    session.send(new ServerboundAcceptTeleportationPacket(p.getTeleportId()));
                }
            }
            @Override public void disconnected(DisconnectedEvent event) { ready.completeExceptionally(new IllegalStateException("Minecraft disconnected")); }
        });
        Runtime.getRuntime().addShutdownHook(new Thread(() -> client.disconnect(Component.text("Super-Bot stopped"))));
        try {
            client.connect(); ready.get(30,TimeUnit.SECONDS);
            String ca=System.getenv("SBP_CA");
            var adapter=new Adapter(new HttpsTransport(System.getenv().getOrDefault("SBP_URL","https://127.0.0.1:8767"),ca==null?null:Path.of(ca)),System.getenv().getOrDefault("SBP_ENVIRONMENT","minecraft-player"),System.getenv().getOrDefault("SBP_AGENT","minecraft-player"),Capabilities.player(),(cap,input)->{
                if(!client.isConnected()) throw new IllegalStateException("Disconnected");
                return position.get().deepCopy();
            });
            adapter.connect(required("SBP_ADAPTER_TOKEN"));
            while(client.isConnected()) { adapter.poll();adapter.observe(position.get().deepCopy());Thread.sleep(2000); }
        } finally { client.disconnect(Component.text("Super-Bot stopped")); }
    }
}
