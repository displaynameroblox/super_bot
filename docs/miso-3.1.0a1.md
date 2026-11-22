# Super-Bot 3.1.0a1 Miso integration checklist

Start with the [release notes](releases/v3.1.0a1.md). The root session node supports Discord, a managed Minecraft SBP bridge, and an opt-in Roblox bridge for private test experiences.

## Minecraft Java

Follow the [Minecraft SDK startup guide](../sdks/minecraft/STARTUP_GUIDE.md) for the mod. Set `SUPER_BOT_SBP_CONFIG` in the managed instance environment and start the node with `super-bot-manager start`. Configure the mod with the node's HTTPS URL, trusted TLS certificate, and an SBP adapter credential. Start Minecraft with the mod installed. Use `super-bot-manager debug platforms` to check bridge configuration and an authorized SDK turn to verify in-world presence.

## Discord and shared memory setup

1. Install the checkout with `python -m pip install -e '.[discord]'`. Back up and select the **existing** TUI `client.db` for the node URL you use. See [Shared Miso memory](miso-memory.md) for database layout and legacy import. Enable or upgrade the existing schema on the authority machine.

   ```sh
   python -m Ai.miso_admin --db "$MISO_DB" enable
   ```

2. Start `super-bot-server` with `SUPER_BOT_MEMORY_API_DB` pointing to that absolute database path. Set `PHONE_MODEL_PROVIDER` and the corresponding model and credentials on the **node**. Use an existing local model endpoint for a low-cost smoke test. A cloud provider receives the bounded conversation context sent for each inference call.
3. Start `super-bot --url https://127.0.0.1:8000`, substituting your node URL. Verify both fingerprints. Sign in to the owner account, select the saved Miso profile with `/profile use Miso`, then run `/account publish-miso`. Start a new signed-in chat with that profile. Publishing updates the owner profile in the same database. Existing TUI sessions retain their saved configuration. Publish again after changes and open a fresh Miso session to compare results.
4. On the node set `DISCORD_OWNER_ID`, optional `DISCORD_CHANNEL_IDS`, and a private randomly generated `SUPER_BOT_DISCORD_NODE_TOKEN` of at least 32 characters. For example, generate one in a private terminal with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Store it privately on the node and gateway hosts. Restart the node when changing these settings. The node reuses the existing Discord history/rate-limit store. Set `SUPER_BOT_DISCORD_DATA_DIR` on the node if the existing store is elsewhere, and stop any legacy bot process using it.
5. In the Discord gateway's private `.discord.env`, set the same node token, `SUPER_BOT_DISCORD_NODE_URL`, bot token, owner, and channel IDs. Verify the node fingerprints once under the same OS account and trust-store configuration that will run Discord. The TUI or account client can do this. Run `super-bot-discord`. In the signed-in TUI run `/account link-discord YOUR_DISCORD_ID` and send `/link CODE` in the owner DM. The existing link can be reused if it already points to this database. Startup prints the node's configured model when available. Each message makes a fresh sealed request, so the bot can recover on a later message after a node restart. The existing Discord reconnect logic handles gateway disconnects.

## Roblox closed test

1. For a published private test experience you control, publish only character settings and notes suitable for the experience. In a private terminal issue a grant for its numeric **universe ID** and the single approved Roblox **user ID**. Provision each approved experience separately.

   ```sh
   python -m Ai.miso_admin --db "$MISO_DB" issue-roblox YOUR_USERNAME UNIVERSE_ID USER_ID
   python -m Ai.miso_admin --db "$MISO_DB" share-roblox-note YOUR_USERNAME
   ```

   The second command is optional. It prompts privately for one explicit shared note. Roblox sees up to four most recent shared notes and its own session context. Linked private clients can recall the same notes. The grant expires in seven days. Revoke immediately with `python -m Ai.miso_admin --db "$MISO_DB" revoke-roblox YOUR_USERNAME GRANT_ID`, then remove or rotate the experience secret. Keep a private record of the grant ID. Rotate grants after restoring a backup that might contain previously valid credentials.

2. Set `SUPER_BOT_ROBLOX_BRIDGE=1` on the authority node. Use an approved reverse proxy with a valid public HTTPS certificate, exactly two allowed POST paths (`/miso/roblox/status`, `/miso/roblox/chat`), an ingress request limit, and a 16 KiB body limit. Forward those paths to the local node over verified TLS, including the bearer header. Return 404 for everything else, including the node's sealed endpoints, discovery, and management interfaces. Disable body and authorization logging. The local node can remain bound to loopback. Roblox game servers need the reachable public origin and cannot use your laptop's loopback or private LAN address. No proxy or hosting is provisioned here. [Roblox HTTP requests](https://create.roblox.com/docs/cloud-services/http-service)
3. Enable **Allow HTTP Requests** in the test experience. Add the grant as an experience secret, for example `SUPER_BOT_MISO`, restricted to the public proxy hostname. The Rojo project places the SDK at `ServerScriptService.Miso`. Require it from a server script and create an instance only after checking the real connected player's ID against your approved audience. For a private UI, enforce the same audience and a request limit on any remote your game adds. [Roblox secrets store](https://create.roblox.com/docs/cloud-services/secrets)

   ```luau
   local SDK = require(game:GetService("ServerScriptService").Miso)

   -- A trusted server handler supplies player after checking the approved user ID.
   local client = SDK.new("https://miso.example.com", "SUPER_BOT_MISO", player)
   local ready, statusError = client:status()
   local reply, chatError, requestId = client:chat("Hi Miso")
   ```

   Keep one client per approved player session. The SDK gets the universe and player IDs from Roblox and creates the session ID on the server. It returns a structured failure with an optional HTTP status. On transient transport failure, a server handler can retry with the same text and returned request ID after the cooldown. A completed request returns the cached reply. Unresolved reservations return 409 and consume the attempt, including after a process restart. After a 403, check IDs, secret domain, expiry, and revocation. Use status to recheck connectivity. Status checks no inference and spend no model tokens.

   The SDK filters the generated reply for the single recipient and returns no text if filtering fails. Keep the UI private to that player. The SDK adds no remotes and executes no generated game actions. If you later add actions, validate and execute them on the Roblox server against game-owned rules. [Roblox text filtering](https://create.roblox.com/docs/ui/text-filtering)

## Memory and privacy scopes

| Surface | Profile and context |
| --- | --- |
| Signed-in TUI and linked owner DM | The published Miso persona/style/skills, per-client transcripts, and bounded recall from the same owner memory. Existing signed-in TUI sessions contribute to this memory when the feature is enabled. Set `SUPER_BOT_MISO_MEMORY=0` for a TUI process whose chats should skip shared recall and writes. |
| Discord guest DMs and guild channels | Guest DM history stays with that Discord identity. Guild history stays in its configured channel. Both use the default persona and omit the owner's profile and memory. |
| Roblox approved player | The approved grant's published character settings, separate short session history, and explicitly shared notes. Private TUI/Discord transcripts never enter the Roblox prompt. Roblox session history stays outside private TUI/Discord recall. |
| Authentication | TUI uses its local account, Discord owner links with an expiring DM code, and the Discord gateway has a dedicated sealed-route credential. Roblox grants are hashed and scoped to chat/status for one player and universe. They cannot search memory, submit notes, control the node, or act as Discord. |

The node enforces one Roblox inference attempt per 15 seconds and 30 per rolling 24 hours, across all Roblox grants. Failed inference consumes budget. The Roblox request ledger retains recent prompts and replies for deduplication and session recall. Rows older than a day are removed on the next new authorized chat request, and revocation clears a grant's stored text. Existing Discord limits apply independently. Use a provider-side spending limit if selecting a paid model.

## Verify and release

- [ ] Run the local regression and integration tests with a fixture provider.

  ```sh
  python -m pytest -q Ai/tests/test_miso_integration.py Ai/tests/test_miso_memory.py Ai/tests/test_discord_alpha.py Ai/tests/test_gateway.py Ai/tests/test_client_data.py Ai/tests/test_server_sessions.py
  rojo build default.project.json -o /tmp/miso-test.rbxlx
  ```

- [ ] On the configured node, test a distinct fact in a new private TUI chat and linked Discord DM in both directions. Confirm a different Discord account and a guild channel cannot retrieve either fact. Exit the TUI and confirm Discord still works. Stop Discord and confirm TUI still works.
- [ ] In a published private Roblox experience, verify the HTTPS route, secret retrieval, approved player reply, recipient-only filtering, blocked second player, limits, reconnect and revocation. Check that an intentionally shared note is recalled while the two private facts are absent. Confirm Discord and TUI remain independent of Roblox.
- [ ] Verify a chosen real provider using a small request budget. Confirm live platform policy and moderation requirements for the experience before expanding its audience.
- [ ] Decide whether to add authorized presence events or a shared conversation view after alpha. This foundation reads new committed memory on the next turn. It includes no cross-client presence feed or transcript mirroring.
