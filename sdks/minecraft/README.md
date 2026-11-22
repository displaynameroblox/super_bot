# Super-Bot Minecraft SDK — 0.1.0 alpha 3

Java Edition 1.21.1, Java 21, SBP 0.1. This project is independent of the root Roblox `src/` and `default.project.json`; neither is relocated.

For detailed startup, setup, testing, and troubleshooting instructions, see the [SBP and Minecraft startup guide](STARTUP_GUIDE.md).

## What works

| Module | Implemented scope |
| --- | --- |
| `common` | Verified HTTPS with optional CA file, SBP sessions, environment/agent registration, polling, result updates, observations, explicit Chatter task requests |
| `fabric` | Integrated singleplayer server commands; one named villager NPC; state observation, stop navigation, start a native path within 32 blocks |
| `player-agent` | Standalone Minecraft protocol client, authenticated account/token login or local offline test login, teleport acknowledgement, server-reported position observations |

This is an alpha, not an autonomous survival bot. The player client has no walking, physics, mining, inventory, combat, crafting, signed chat or Microsoft sign-in UI. You supply a valid Minecraft Java access token and profile obtained through your own authorized sign-in. Tokens are never accepted as command-line arguments or saved by this SDK. No account credentials are bundled.

Fabric uses the vanilla villager model and navigation, not a custom player skin. The spawned NPC disables the villager brain so it does not replace SDK navigation targets. `minecraft.move_to` completes when a path **starts**, not when the NPC arrives. Its result says `navigation_started: true, arrived: false`; observe `navigation_done` and coordinates separately. No cross-dimension or unloaded-chunk navigation. No dedicated-server NPC support. Commands require cheats/operator permission in your singleplayer world.

## Build

Install JDK 21 and set `JAVA_HOME`. From this directory:

```sh
./gradlew build
# From the repository root, with Super-Bot dependencies installed:
PYTHONPATH=. python sdks/minecraft/tools/check_contract.py
python sdks/minecraft/tools/package_release.py
```

Windows: use `gradlew.bat`. Builds require access to Maven Central, Fabric, Mojang and OpenCollab repositories. Gradle produces the common library and sources, the remapped Fabric mod, and a player-agent ZIP containing Windows/Unix launchers and runtime dependencies. Java itself is not included. Packaged output and checksums go to `build/release/0.1.0-alpha.3/`. CI uploads these as a downloadable artifact; it does not publish to Modrinth, CurseForge or GitHub Releases automatically. No public download listing has been created.

## Backend setup

Use the existing SBP host (default `https://127.0.0.1:8767`) or enable the optional SBP bridge in the normal Super-Bot server as described below. Follow [the repository host setup](../../docs/sbp/configuration.md).

Provision a principal with role `adapter`, environment `minecraft-npc`, and the following `capabilities` **and** `permissions`: `minecraft.observe`, `minecraft.stop`, `minecraft.move_to`. For the standalone player use environment `minecraft-player` and only `minecraft.observe`. Grant knowledge sources `environment` and `user`. Agents are registered under the adapter principal, so use that principal's session (or an administrator) to submit actions. A separate controller cannot control an agent owned by another principal under current SBP ownership rules. Use distinct environment/agent IDs for multiple installations. Run only one adapter worker per environment.

Export these before starting Minecraft or the standalone launcher:

```sh
export SBP_URL=https://127.0.0.1:8767
export SBP_CA=/absolute/path/to/host/tls/tls-certificate.pem
# Load SBP_ADAPTER_TOKEN from your private credential store, not shell history.
```

`SBP_ENVIRONMENT` and `SBP_AGENT` override the defaults (`minecraft-npc` or `minecraft-player`). The CA certificate must be trusted through an independent channel. Hostname verification remains enabled. There is no insecure TLS switch.

### Fabric installation

1. Install Minecraft Java 1.21.1, Fabric Loader 0.16.10 or newer, and Fabric API for 1.21.1.
2. Place the non-sources `superbot-minecraft-fabric-1.21.1-*.jar` from `fabric/build/libs/` in `mods/`. The common library is nested inside it; do not separately install the SDK JAR.
3. Launch Minecraft with the environment settings above, open a singleplayer world with cheats enabled, and run `/superbot spawn`.
4. Use `/superbot status` and `/superbot remove`. Without credentials, spawn still works but SBP stays disconnected.

HTTP runs on a background worker. Native world access runs on the server thread. Leaving the world stops the worker and removes the tracked NPC. NPC state is not recovered across crashes; an NPC saved before a crash may need manual removal.

### Standalone player

Extract `player-agent/build/distributions/superbot-minecraft-player-*.zip`. Set `MC_HOST`, `MC_USERNAME`, `MC_UUID`, `MC_ACCESS_TOKEN`, and the SBP settings above. Optional `MC_PORT` defaults to 25565. Run `bin/superbot-minecraft-player` (Windows: `.bat`). `--help` lists settings. `--offline` permits a local offline-mode test server only; it does not authenticate to online-mode servers. Use a separate licensed account and a server that allows automation.

The client reports the coordinates and relative flags supplied by server position packets; it does not pretend to have a fully reconstructed world. Login, configuration and keepalives are delegated to MCProtocolLib pinned snapshot `1.21-20241010.155958-24` (protocol shared by 1.21/1.21.1). Real server compatibility, account login, and rendering require manual smoke tests; see [limits and testing](docs/limits.md).

## Actions and conversation

[Example requests](examples/actions.json) use the existing SBP routes and schemas in `Ai/universal/contracts.py`. Authenticate via `POST /sbp/v0.1/sessions` with `{"versions":["0.1"]}` and the bootstrap bearer. Use the returned session bearer for all other requests. Submit actions to `POST /sbp/v0.1/agents/{agent_id}/actions`. The backend validates capabilities, grants, input, idempotency and deadlines.

Structured actions do not need a model. Natural chat and AI-directed movement use the backend's configured model. Chatter is not required: the default model route is `existing`, which uses the same `Ai.phone_model` provider as the normal Super-Bot server. The server itself is not model weights; it must already have a working model provider. Set `SBP_MODEL_ROUTE` on the game instance to use another administrator-registered route later.

After spawning the NPC and connecting the backend, use these commands in game (cheats/operator permission required):

```text
/superbot chat Hello, how are you?
/superbot move come to me
/superbot move walk three blocks east
```

The model sees the player's and NPC's positions at request time. It must return exactly one JSON destination (plain or in a single JSON code fence); the adapter rejects malformed, extra-keyed, non-finite, out-of-bounds or more than 32-block plans, then submits an ordinary authorized `minecraft.move_to` action. The NPC tells you whether navigation started, failed, or remains pending. It is not continuous following, and it cannot guarantee arrival or avoid every hazard. The commands do not intercept ordinary chat. One model request runs at a time; responses are private to the requesting player. On a Phase 2 host, private chat resumes the saved conversation for the player, world and NPC. Use `/superbot session new` to clear it. Older hosts provide single-turn chat.

### Use the normal Super-Bot server for Minecraft

The normal server can optionally expose SBP over its existing HTTPS listener alongside its encrypted chat gateway. SBP requests still use their own adapter bearer credential and authorization. Configure the existing private SBP configuration with `"existing_model_provider": true` at the top level and add `"existing"` to the adapter principal's `models` list. The original `minecraft.observe`, `minecraft.move_to`, and `minecraft.stop` grants and permissions are still required.

Start the normal server with the opt-in configuration path (the port remains whatever the normal server is configured to use; its default is 8000):

```sh
SUPER_BOT_SBP_CONFIG="$HOME/.superbot-minecraft/config.json" \
  .venv/bin/python -m Ai.server
```

If the local manager starts the selected model, set the same opt-in variable when starting or restarting the **updated checkout** through the manager:

```sh
SUPER_BOT_SBP_CONFIG="$HOME/.superbot-minecraft/config.json" \
  .venv/bin/python -m Ai.manager_cli restart
```

Restarting interrupts current sessions. This variable must be present on future manager starts too (for example, in the manager instance's private `.env`). The opt-in bridge passes the selected managed local model's temporary endpoint and key to the server for its lifetime. If using an installed or signed release, it must first be updated to a version that contains this bridge; starting an older release will not add the SBP routes.

For the integrated mode, set the Minecraft instance's `SBP_URL` to the normal server HTTPS origin (for example `https://127.0.0.1:8000`) and `SBP_CA` to **that server's** certificate, not the separate SBP host's certificate. On macOS the default generated certificate is `~/Library/Application Support/super_bot/server/tls-certificate.pem`; for the default managed instance it is `~/Library/Application Support/super_bot/instances/default/data/tls-certificate.pem`. Use an absolute path in Modrinth. Set `SBP_ADAPTER_TOKEN` to the matching private adapter token. The integrated server keeps its SBP database in a private `sbp` folder under the normal server data directory; it does not share the standalone host's database. Existing chat routes stay end-to-end encrypted as before. The SBP routes are authenticated separately and protected by the server's HTTPS certificate; enabling the bridge also exposes those routes wherever that server listens.

The normal server's configured `Ai.phone_model` provider is reused directly, including a local model started by the local manager. When this bridge is enabled, the manager supplies its selected local model endpoint and credential to the same server process. The standalone SBP host also supports this route when `existing_model_provider` is enabled; in that opt-in case it loads the same `.env` locations as the normal server. If no model is configured or reachable, chat and AI movement report an error, while structured actions remain available. Check [normal server model configuration](../../README.md) and avoid sending sensitive information to an untrusted or remote provider.

On transport/authentication errors the supplied launchers stop polling rather than retrying world actions. Restart the player process, or remove/spawn the NPC, to reauthenticate (including after the default one-hour session expiry). A completed result can be retried within the same adapter instance without rerunning native execution. Acknowledged/running actions recovered without a local result fail as `execution_uncertain`. This favors avoiding duplicate effects over automatic recovery; it does not provide transactional exactly-once world execution. Cancellation is not advertised for these short operations; stop navigation is a separate action.

## Embedding the common SDK

Depend on the `common` Gradle project (or its built JAR plus Gson 2.11.0). Implement `Adapter.NativeActions`, advertise only the capabilities you actually execute, then call `connect`, `poll` and `observe` from a single background worker. Marshal native game operations onto the engine's thread. The standalone distribution already includes its runtime dependencies; the Fabric mod uses Minecraft's Gson and embeds the common module.
