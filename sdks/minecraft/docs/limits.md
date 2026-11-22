# Validation and release checklist

Automated checks:
- Common JUnit tests cover successful lifecycle, lost result delivery, ambiguous in-flight recovery, native failure, agent binding and HTTPS origin validation.
- `tools/check_contract.py` validates Java-generated capability schemas against the Python contracts and real host state machine; executes a model-free task and verifies that missing Chatter fails explicitly.
- `tools/https_smoke.py` starts a temporary TLS host and exercises Java session registration, polling, action completion and observation delivery over real HTTPS.
- Gradle compiles both native integrations and produces distribution artifacts.
- CI runs the existing universal host regression suite as well.

Manual release checks (not replaced by compilation):
1. Start a fresh singleplayer Fabric 1.21.1 world, spawn/remove the NPC, reconnect, close/reopen the world, and confirm no worker remains.
2. Submit observe/move/stop against a local HTTPS host. Check unreachable, distant and unloaded targets; confirm movement completion means path initiation.
3. Pause the game, disconnect the backend, expire/revoke credentials, and ensure failures are visible without repeated native effects.
4. Join an authorized 1.21.1 server with a real account and verify configuration, keepalives, teleport acknowledgement and position updates. Test kicks, token expiry and shutdown.
5. Verify Chatter route unavailable/available behavior against the actual deployed Chatter provider.

Not implemented: graphical setup, discovery, automatic reconnection, Microsoft OAuth/device code, chat signing, world/chunk reconstruction, movement physics for the player, inventory, block interaction, combat, crafting, path planning for the player, skin rendering, multi-NPC persistence, guaranteed arrival, modpack compatibility or other Minecraft versions. Do not publish this alpha as a complete survival agent.

Dependency references: [Fabric 1.21.1 reference build](https://github.com/FabricMC/fabric-docs/blob/main/reference/1.21.1/build.gradle), [MCProtocolLib 1.21-1 example](https://github.com/GeyserMC/MCProtocolLib/blob/1.21-1/example/src/main/java/org/geysermc/mcprotocollib/protocol/example/MinecraftProtocolTest.java). Upstream dependencies retain their own licenses. No Minecraft game binaries are redistributed.
