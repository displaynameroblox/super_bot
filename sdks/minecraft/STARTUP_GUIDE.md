# Starting Super-Bot and the Minecraft SDK

This guide covers the **Minecraft Java 1.21.1 Fabric NPC**, SBP v0.1, and the alpha 3 SDK. It uses the existing Super-Bot server and local model through the local manager. A standalone SBP host is covered later. The examples use macOS and a source checkout; see the Windows notes near the end.

The Roblox `src/` and Rojo project are separate and do not need to be moved.

## 1. How the pieces fit together

```text
Minecraft singleplayer world
  └─ Fabric mod: NPC, commands, observation and navigation
       │ HTTPS + SBP adapter credential
       ▼
Super-Bot Python server, with SBP enabled
  ├─ validates agent permissions and queues structured actions
  └─ passes chat / movement planning to its configured model provider
       ▼
Optional local language model
```

**SBP** is the Super-Bot Protocol. It is the way the mod registers its environment, reports observations, receives actions and sends results. It is not itself a language model. The SDK is the code and adapters that connect Minecraft to SBP.

The normal Super-Bot server can host SBP on its existing HTTPS port. Alternatively, you can start a separate SBP host. Pick one approach for a particular Minecraft instance: use its matching URL, certificate and credentials together.

- Structured actions such as a specific coordinate request **do not require a model**.
- `/superbot chat` and AI-directed `/superbot move` **do require a configured, reachable model**. Chatter is not required; the tested setup uses the server's `existing` model route. Chatter can be added later as an explicitly registered route.
- The model is allowed to propose a destination, not arbitrary shell commands or Minecraft commands. SBP permissions and the mod's coordinate/distance checks still apply.

## 2. Requirements

- Minecraft **Java Edition 1.21.1**, Fabric Loader **0.16.10 or later**, and Fabric API compatible with 1.21.1.
- Java **21** for Minecraft and the SDK build.
- The updated Super-Bot repository, Python 3.10 or newer, and its dependencies. Examples below use the checkout's `.venv/bin/python`; if you use a different environment, substitute its Python. To create an environment from the repository root:

  ```sh
  python3 -m venv .venv
  .venv/bin/python -m pip install -e .
  ```

- A compatible model configured for the normal Super-Bot server if you want AI chat or AI movement. The local manager can start a selected local model; ordinary server configuration is explained in the [main README](../../README.md). Starting the Python server alone does not download a model.
- A private SBP configuration and adapter token, created in step 3.

## 3. One-time SBP configuration

The working setup from the earlier test uses `~/.superbot-minecraft/` and the adapter ID `minecraft-sdk`. If you already created it, **keep the existing token and configuration**. Do not rerun the creation script or replace the token: Modrinth and the backend must use the same one.

For a fresh setup only, run this once in Terminal from the repository root. It creates a private directory, a configuration containing a hash of a new random token, and a separate token file. It refuses to overwrite either file. It grants only the listed Minecraft environment, capabilities, permissions, and the `existing` model route.

```sh
.venv/bin/python - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path

root = Path.home() / '.superbot-minecraft'
root.mkdir(mode=0o700, exist_ok=True)
if os.name != 'nt' and root.stat().st_mode & 0o077:
    raise SystemExit(f'{root} must be private: chmod 700 {root}')
if (root / 'config.json').exists() or (root / 'token').exists():
    raise SystemExit('Configuration or token already exists; nothing was overwritten.')

token = secrets.token_urlsafe(32)
caps = ['minecraft.observe', 'minecraft.stop', 'minecraft.move_to']
config = {
    'existing_model_provider': True,
    'principals': [{
        'id': 'minecraft-sdk',
        'role': 'adapter',
        'token_hash': hashlib.sha256(token.encode()).hexdigest(),
        'environments': ['minecraft-npc'],
        'capabilities': caps,
        'permissions': caps,
        'knowledge_sources': ['environment', 'user'],
        'models': ['existing'],
    }],
}
for name, content in [('config.json', json.dumps(config, indent=2)), ('token', token)]:
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(content)
print('Private Minecraft configuration created in', root)
PY
```

If creation is interrupted after only one file is created, inspect the directory before trying again. Do not share the token, commit it to Git, or put it in screenshots. On Windows, protect the configuration and token with equivalent private file permissions.

For an existing configuration, enable `"existing_model_provider": true` at the top level and add `"existing"` to the Minecraft adapter's `models` list to enable model requests. Preserve its token hash and other grants. Changes require a server restart. The [SBP configuration guide](../../docs/sbp/configuration.md) describes principals, grants and certificate setup in detail.

## 4. Start the normal Super-Bot server with SBP

### Recommended: local manager and selected local model

From the repository root:

```sh
SUPER_BOT_SBP_CONFIG="$HOME/.superbot-minecraft/config.json" \
  .venv/bin/python -m Ai.manager_cli start
```

If the managed server is already running with the old code or settings, **restart** instead of starting another copy:

```sh
SUPER_BOT_SBP_CONFIG="$HOME/.superbot-minecraft/config.json" \
  .venv/bin/python -m Ai.manager_cli restart
```

A restart disconnects current sessions. The variable must be provided on future starts as well, or saved as `SUPER_BOT_SBP_CONFIG=/absolute/path/to/config.json` in the manager instance's private `.env`. In the tested Mac setup, this was already added to the default manager instance. The bridge is opt-in; enabling it exposes the SBP routes wherever the normal server listens, protected by HTTPS and separate SBP credentials.

Check status:

```sh
.venv/bin/python -m Ai.manager_cli --json status
```

Look for `"state": "healthy"`. The tested normal server listens on `https://127.0.0.1:8000`. Your `PORT` setting can differ. If you use an installed/signed release rather than this checkout, it must contain the SBP bridge; an older running process will not gain new code until updated and restarted.

The manager may launch a selected local model alongside the server. With the SBP bridge enabled, the manager supplies its temporary model endpoint and credential to the server. A selected model and a healthy server are separate things: model loading or inference can still fail.

### Direct server startup

If your model provider is already configured in your environment or repository `.env`, you can run the normal server directly:

```sh
SUPER_BOT_SBP_CONFIG="$HOME/.superbot-minecraft/config.json" \
  .venv/bin/python -m Ai.server
```

Leave this Terminal running. This command does not automatically start a local model; the manager route above can do so when a model is selected. Do not run a second server on a port already in use.

### Verify SBP is reachable

For the default **managed** server on macOS, the certificate is normally here:

```text
~/Library/Application Support/super_bot/instances/default/data/tls-certificate.pem
```

The direct server normally uses `~/Library/Application Support/super_bot/server/tls-certificate.pem`. A custom `SUPER_BOT_SERVER_DIR` or `SSL_CERTFILE` changes the location. Use the actual certificate belonging to the server you started.

For the default managed server and port:

```sh
curl --cacert "$HOME/Library/Application Support/super_bot/instances/default/data/tls-certificate.pem" \
  https://127.0.0.1:8000/sbp/v0.1/protocol
```

A successful response contains `"versions":["0.1"]`. If the normal `/health` works but this route returns 404, the running server does not have the bridge enabled or is running older code. Certificate and hostname verification should remain enabled; obtain/trust the certificate through a trusted channel.

## 5. Install the Fabric mod in Modrinth

1. Create or choose a **separate Fabric 1.21.1 instance** in Modrinth. This Fabric mod does not load in a NeoForge-only instance. Keep your existing NeoForge worlds and mods in their own instance.
2. Install **Fabric API for 1.21.1** into that instance.
3. Add the locally built `superbot-minecraft-fabric-1.21.1-0.1.0-alpha.3.jar` from `build/release/0.1.0-alpha.3/`, or the non-sources JAR from `fabric/build/libs/`. Modrinth may warn that a locally built file is not published or reviewed there.
4. Enable **only one version** of Super-Bot Minecraft. Disable older alpha JARs to avoid duplicate mod IDs. Do not install the `-sources.jar` or the common SDK JAR as separate mods.
5. Select Java 21 in the instance if needed.

The [Minecraft SDK README](README.md) and [validation notes](docs/validation.md) contain the build and testing details. The JAR is an alpha artifact from this repository; it has not been published to Modrinth.

## 6. Connect the Modrinth instance

Open **Instance settings → Java and memory → Custom environment variables** (the label may be **Environment variables** in newer versions). Set the following values for the **normal managed server**:

| Name | Value |
| --- | --- |
| `SBP_URL` | `https://127.0.0.1:8000` if the server is on the default port |
| `SBP_CA` | Absolute path to the normal server's public certificate |
| `SBP_ADAPTER_TOKEN` | Contents of `~/.superbot-minecraft/token` |
| `SBP_MODEL_ROUTE` | Optional; `existing` is the default |

Use the same environment ID and agent ID as the backend grants. The defaults are both `minecraft-npc`; optional overrides are `SBP_ENVIRONMENT` and `SBP_AGENT`. Different instances need different authorized IDs. Run only one adapter worker per environment.

**Paths with spaces:** the tested Modrinth settings field split an unquoted certificate path at the space. On this Mac, a verified copy of the normal server's *public* certificate was placed at:

```text
/Users/display_name/.superbot-minecraft/normal-server-cert.pem
```

For a fresh setup, copy the actual public certificate to a private directory using a path without spaces, or use the launcher's supported quoting. A copy must be refreshed if the server certificate changes. Never copy `tls-private-key.pem` into the Minecraft instance. When connecting to a remote host, obtain and verify its public certificate independently.

To copy the adapter token into the clipboard on macOS without printing it:

```sh
pbcopy < "$HOME/.superbot-minecraft/token"
```

Paste it as the value of `SBP_ADAPTER_TOKEN`; keep it private. Save the instance settings and **fully restart Minecraft**: an already-running game cannot inherit changed environment variables. The token grants the configured adapter capabilities, so protect Modrinth's instance settings and do not export the token in a shared modpack.

## 7. Spawn the NPC and test it

With the Phase 2 backend and Fabric build, `/superbot status` shows
**SBP connected; private chat history enabled**. Use `/superbot chat` as usual.
The latest successful turns are stored in the SBP database for your player,
save and NPC, and are reused when you reconnect. The model receives a short
recent context to fit the local runner. Use `/superbot session new` to clear
your conversation. A copy of a world save also copies its
`superbot-world-id` file. Remove that file from the copy before opening it
if the copy should have its own chat history.

Older SBP builds continue to support single-turn chat. To enable persistent
chat, run a backend build containing Phase 2 and install the matching Fabric
JAR. The backend stores up to 24 successful turns per conversation.

If the manager's server is already running with SBP enabled, leave it running.
Set `SBP_URL` to its HTTPS origin (for example, `https://icarus.lan:8000`)
and use that server's matching `SBP_CA` and adapter credential. The certificate
must cover the name in the URL. The mod verifies the host before sending the
credential. On an updated host, get the `node_id` from
`GET /.well-known/superbot` over trusted HTTPS and set `SBP_NODE_ID` to pin it.
With a node ID set, the mod requires that descriptor. Otherwise an older
host can still connect if its HTTPS SBP 0.1 protocol probe succeeds.

Keep the model route set to `existing` and grant it to the adapter. The running
manager instance supplies its selected local GGUF or configured Gemini
provider through that route. Minecraft needs neither the GGUF path nor the
Gemini API key. `/superbot status` distinguishes a missing model route from
a disconnected host. Model inference can still fail independently of the route.

Start the backend first, then launch the Fabric instance. Open a **singleplayer** world with cheats/operator permissions enabled. In the in-game chat, run:

```text
/superbot spawn
/superbot status
```

`SBP connected` means the adapter registered successfully and is polling the selected backend. Without credentials the villager can still spawn, but it cannot communicate with SBP. If you change settings or restart the server, reconnect it:

```text
/superbot remove
/superbot spawn
/superbot status
```

To test the model route:

```text
/superbot chat Hello!
/superbot move come to me
```

The chat reply is sent privately to the requesting player. Requests use commands; ordinary player chat is not automatically forwarded. A movement request proposes **one** nearby destination, with a 32-block limit. The mod reports whether navigation started, failed, or is pending. `Navigation started` is not proof of arrival. It does not continuously follow you, and the model cannot see a full map of obstacles.

The tested setup returned **“Greetings!”** in game and the user observed the NPC move; the backend recorded a completed action whose result was `navigation_started: true, arrived: false`. Other models can produce invalid plans, which are rejected without execution.

## 8. Send structured actions without a model

A controller can authenticate using `POST /sbp/v0.1/sessions` with the bootstrap bearer token and body `{"versions":["0.1"]}`. Use the returned **session** bearer for other operations. Submit an action to `POST /sbp/v0.1/agents/minecraft-npc/actions`. The adapter and agent must already be registered and authorized.

For example, the body for a move is:

```json
{
  "id": "move-unique-001",
  "environment_id": "minecraft-npc",
  "capability": "minecraft.move_to",
  "version": "1",
  "input": {"x": 10, "y": 64, "z": 10}
}
```

Use a **new unique action ID** for each new request. Reusing one with changed input is rejected. Coordinates must be valid and within 32 blocks of the NPC in its current dimension. `minecraft.observe` and `minecraft.stop` use an empty input object. Results and observations are available through the SBP APIs; see [example requests](examples/actions.json) and the [SBP API reference](../../docs/sbp/api-reference.md). The backend validates schema and grants; the game adapter separately checks native limits.

## 9. Alternative: standalone SBP host

This is a separate process and database, not the normal server's embedded bridge. Use it when you specifically want a standalone SBP endpoint:

```sh
.venv/bin/python -m Ai.universal.http \
  --config "$HOME/.superbot-minecraft/config.json" \
  --data-dir "$HOME/.superbot-minecraft/host"
```

Leave it running. Its default address is `https://127.0.0.1:8767`, and its generated certificate is `~/.superbot-minecraft/host/tls/tls-certificate.pem`. Set the Modrinth `SBP_URL` and `SBP_CA` to **these** values rather than the normal server values; the adapter token still must match the chosen configuration. The standalone and embedded hosts use separate databases, so resources and history are not automatically shared.

With `existing_model_provider` enabled, the standalone host uses the configured `Ai.phone_model` provider and loads the normal server's `.env` locations. It does **not** automatically borrow a separately managed server's temporary local-model process or start model weights itself. Configure and run a model endpoint for it if you need chat or AI movement.

## 10. Troubleshooting

**`SBP disconnected` / `IOException`:** Verify the correct host is running, the URL uses the correct port, `SBP_CA` is the certificate for that exact server, and the token matches `config.json`. The host's audit log can distinguish rejected credentials from transport failures. Check for spaces splitting environment values. Restart Minecraft after editing launch settings, then remove and respawn the NPC.

**HTTP 401 or 403:** The bootstrap token may not match the stored hash, a session may have expired, or the principal may lack the environment, capability, permission, knowledge-source or model grant. Do not paste a token into a public log. The default session lasts one hour; remove and respawn to reauthenticate after expiry.

**Model request 422 / unavailable:** The `existing` route is opt-in and the principal must be granted `existing` in `models`. Confirm the server was restarted after changing configuration. A healthy HTTPS server does not prove a model is running.

**Model request 502:** Check the server and model logs for unavailable provider, memory, or context-window errors. Repeated Minecraft observations previously overflowed a small 512-token model; the updated backend bounds evidence and omits redundant history from movement planning. Restart the updated Python server to load that fix. Chat and movement still require a model that can respond within its available context.

**`Model must return a JSON destination`:** Check you are running alpha 3. It accepts a single plain JSON object or one JSON code fence and rejects extra keys, malformed output, non-numeric values and unsafe distances. A very small model may still fail to follow the requested format. The previous backend context overflow could also truncate its output.

**NPC says it may not reach the destination:** That message is expected when navigation starts. The adapter cannot yet guarantee arrival. Try a clear, loaded area, nearby coordinates and the same dimension.

**`stop_timeout` on restart:** Check `server.log` and available disk space. A previous full-disk telemetry write stopped the shutdown-control thread; the updated worker tolerates that write failure. Free space safely as appropriate; do not delete worlds or configuration just to make room. If an old process remains stuck, identify it and its active sessions before stopping it.

**Game or server is paused/disconnected:** The NPC's native world operations require the integrated server thread. The adapter stops on transport errors rather than automatically replaying uncertain actions. Reconnect after restoring the backend. Completed native effects cannot be guaranteed exactly once across a crash.

## 11. Build and multiplayer agent notes

To rebuild from the checkout:

```sh
cd sdks/minecraft
./gradlew build
cd ../..
PYTHONPATH=. .venv/bin/python sdks/minecraft/tools/check_contract.py
.venv/bin/python sdks/minecraft/tools/package_release.py
```

The release folder includes the Fabric mod, common SDK JAR, player-agent distribution, documentation archive and `SHA256SUMS`. Build dependencies require network access on the first build. CI builds and uploads artifacts but does not publish them automatically.

The separate player-agent ZIP connects to a Java multiplayer server as a protocol client. It currently supports login and server-reported position observations; it has **no movement physics, survival actions, signed chat or Microsoft sign-in UI**. It needs a valid Minecraft Java profile and access token supplied separately, or `--offline` for a local offline-mode test server. It is not the Fabric NPC. See the [SDK README](README.md) and [limits](docs/limits.md) before using it.

To start that prototype, first provision a **separate** adapter principal with permission for environment `minecraft-player` and capability **and permission** `minecraft.observe`; bind its agent to that environment. Use its own private bootstrap token. Then extract `superbot-minecraft-player-0.1.0-alpha.3.zip`, set these environment values for the process, and launch its included script:

| Variable | Meaning |
| --- | --- |
| `MC_HOST` | Minecraft server hostname |
| `MC_PORT` | Minecraft server port, default `25565` |
| `MC_USERNAME` | Account profile name |
| `MC_UUID` | Account profile UUID for authenticated mode |
| `MC_ACCESS_TOKEN` | Valid **Minecraft Java access token** for authenticated mode |
| `SBP_URL`, `SBP_CA`, `SBP_ADAPTER_TOKEN` | HTTPS origin, matching public certificate and that player adapter's private SBP token |
| `SBP_ENVIRONMENT`, `SBP_AGENT` | Optional overrides; both default to `minecraft-player` |

From the extracted distribution, run `bin/superbot-minecraft-player` on macOS/Linux or `bin\superbot-minecraft-player.bat` on Windows. `--help` prints the variable names. For a **local offline-mode test server only**, pass `--offline`; authenticated-mode UUID and access token are then not required. The program restricts offline mode to localhost. A real online-mode server requires a separately obtained eligible Java account session and may enforce its own policies. Keep both tokens out of command arguments, logs, shared launcher settings and version control. Authenticated multiplayer login has not yet been verified against a live server.

On Windows use `gradlew.bat` and an appropriate Python environment path instead of `.venv/bin/python`. Environment-variable and clipboard commands differ; configure equivalent values in the launcher, and protect the credential files using Windows file permissions.
