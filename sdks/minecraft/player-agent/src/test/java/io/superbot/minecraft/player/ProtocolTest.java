package io.superbot.minecraft.player;
import org.junit.jupiter.api.Test;
import org.geysermc.mcprotocollib.protocol.codec.MinecraftCodec;
import static org.junit.jupiter.api.Assertions.*;
class ProtocolTest {
    @Test void pinnedLibrarySpeaks1211() { assertEquals(767,MinecraftCodec.CODEC.getProtocolVersion()); }
    @Test void helpDoesNotNeedCredentialsOrNetwork() throws Exception { Main.main(new String[]{"--help"}); }
}
