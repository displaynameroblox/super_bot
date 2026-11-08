# Authentication and local configuration

SBP is an opt-in process, separate from the existing chat server and its account system. Python 3.10+ and the project dependencies are required. No model, cloud account, engine or GPU is needed for protocol operation.

Install from the checkout with `python -m pip install -e .`. The new commands are `super-bot-host` and `super-bot-mock`. You can also use `python -m Ai.universal.http` and `python -m Ai.universal.mock` without reinstalling entry points.

## Create a private local configuration

Run this from a private working directory. It creates new random credentials with exclusive file creation, so an existing configuration is never overwritten. Do not commit the directory or copy its credentials into an SDK source tree.

```python
import json, os, secrets, hashlib
from pathlib import Path
root = Path.home() / ".super-bot-sbp"
root.mkdir(mode=0o700, exist_ok=True)
tokens = {name: secrets.token_urlsafe(32) for name in ("admin", "operator", "game-sdk", "robot-sdk")}
principals = []
for name, token in tokens.items():
    adapter = name.endswith("-sdk")
    principals.append({
        "id": name,
        "role": "admin" if name == "admin" else "adapter" if adapter else "controller",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "environments": ["game" if name == "game-sdk" else "robot"] if adapter else ["game", "robot"],
        "capabilities": ["navigation.move_to"],
        "permissions": ["move"],
        "models": []
    })
for name, value in [("config.json", {"principals": principals}), ("credentials.json", tokens)]:
    with os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        json.dump(value, file, indent=2)
print(root)  # paths only; no credential output
```

Start the host:

```sh
super-bot-host --config "$HOME/.super-bot-sbp/config.json" \
  --data-dir "$HOME/.super-bot-sbp/host"
```

Default: `https://127.0.0.1:8767`. The host generates a private TLS certificate and prints its SHA-256 fingerprint. For custom TLS, set both `SSL_CERTFILE` and `SSL_KEYFILE`; optionally `SSL_KEYFILE_PASSWORD`. Copy/trust the public certificate through an independently trusted channel. The SDK uses certificate and hostname verification; it has no `verify=False` switch.

To listen beyond loopback, explicitly supply `--host ADDRESS --allow-lan`. Run one process/worker per SBP database. A network-facing deployment also needs firewall rules and operational request/time limits. No mesh or multiprocess coordination is implemented.

## Credentials and authority

- Configuration stores SHA-256 digests of **random high-entropy tokens**, never passwords. Hashing low-entropy passwords this way is not supported.
- `POST /sbp/v0.1/sessions` authenticates a bootstrap token in `Authorization: Bearer TOKEN` and negotiates `versions`. Other protected routes require the returned session token. Bootstrap credentials cannot call ordinary operations directly.
- Principals have role `admin`, `controller`, or `adapter`. Environments, capability IDs, permission names, model route names and knowledge source categories are separately scoped. Exact strings or `"*"` may be configured; use exact scopes for ordinary principals. Admin bypasses these scope lists but cannot act as an environment adapter.
- Only adapters register/execute their own environments. Controllers and adapters may create agents they own. Agents can have zero environments or explicit bindings to multiple authorized environments. Bindings and action capability grants are immutable in this minimal version; create a new agent for a different configuration.
- `POST /agents/{id}/sessions` returns an agent-bound token for the owner. It cannot create agents, mint more credentials or access another agent's data. This supplies independent agent authentication without provisioning a separate bootstrap token per agent.
- Default sessions expire after 3,600 seconds. Reconnect rotates the token, retains the session ID and extends expiration. The old token stops working immediately. After expiration or a lost rotation response, reauthenticate with the bootstrap credential; resource IDs and state persist, but the new session has a new ID.
- A session can revoke itself. An admin may revoke any session or principal. Principal revocation is persisted and invalidates all its sessions and pending actions; queued action authorization is rechecked on poll/update/read. Revoking a single session prevents further use of that credential; it does not withdraw previously authorized actions. Cancel actions or pause/stop the agent to withdraw execution authority.
- Configuration changes take effect on restart. Removing a principal removes authorization; replacing its bootstrap hash does not invalidate still-valid session tokens. Use explicit revocation for immediate incident response. Persisted principal revocation is permanent in v0.1; provision a new principal ID for recovery.
- Host configuration requires POSIX mode `600`; the host data directory requires `700`. SQLite files are `600`. On Windows, provision equivalent private ACLs. Data is plaintext at rest; use OS disk encryption and protect backups. Session tokens are stored only as digests on the host. Adapters must protect their own session tokens and local execution ledgers.
- Audit records contain principal, operation, resource ID, timestamp and outcome, not bodies or bearer tokens. Reads are admin-only. These local logs are bounded and not tamper-evident.

## Resource limits

Optional `limits` configuration keys (positive integers):

| Key | Default | Meaning |
| --- | ---: | --- |
| `session_seconds` | 3600 | Session lifetime |
| `requests_per_minute` | 600 | Fixed-minute budget per authenticated principal; global HTTP ingress budget is 10× this |
| `max_sessions` | 32 | Active sessions per principal |
| `max_agents` | 64 | Agents per principal |
| `max_environments` | 16 | Environments per principal |
| `max_actions_per_agent` | 1000 | All retained actions, including completed ones |
| `max_events_per_agent` | 2000 | Ring retention per agent |
| `max_knowledge_per_agent` | 256 | Knowledge records authored per agent |
| `max_tasks_per_agent` | 256 | All retained tasks per agent |
| `max_body_bytes` | 65536 | Streaming HTTP request-body cap |
| `audit_entries` | 10000 | Global retained audit entries |
| `inference_concurrency` | 4 | Concurrent task/model slots |

Capability constraints additionally bound concurrent outstanding actions per environment/capability (default 8) and deadlines (default 60 seconds). Capability payloads are limited to 32,768 serialized characters. JSON request nesting is limited to 24; schemas to 8,192 characters and 12 nested schema levels. Quotas reject new work; the minimal version has no archive/delete administration API for actions, tasks or knowledge. Plan capacity accordingly.

## Optional model routing

The host defaults to no registered model routes. Tasks without `model` can dispatch explicit authorized action plans without inference. Set `"existing_model_provider": true` to expose route `existing` through `Ai.phone_model.chat`; grant `"models": ["existing"]` to selected principals. Configure the existing provider using its documented environment variables. SBP does not auto-load the chat server's `.env` or download models. A local provider keeps inference local; an external provider receives the bounded authorized context.

Embedders can call `Runtime.register_model(name, provider)` with any object implementing async `generate(messages) -> str`. Chatter tiers, local runtimes, coding or multimodal models can be integrated through this interface without changing SBP. Coding-provider wrappers must preserve the existing command/patch approval controls; none is auto-installed here.

## Run the mocks

Set `SBP_ADAPTER_TOKEN` from the private credentials file in each adapter's process environment, without putting the token in a command-line argument or source file. Then run, in separate terminals:

```sh
super-bot-mock --url https://127.0.0.1:8767 \
  --ca "$HOME/.super-bot-sbp/host/tls/tls-certificate.pem" \
  --environment game --kind game --agent explorer \
  --ledger "$HOME/.super-bot-sbp/game-ledger.db"

super-bot-mock --url https://127.0.0.1:8767 \
  --ca "$HOME/.super-bot-sbp/host/tls/tls-certificate.pem" \
  --environment robot --kind robotics --agent helper \
  --ledger "$HOME/.super-bot-sbp/robot-ledger.db"
```

These commands create adapter-owned agents. A controller can instead register its own agents bound to those environments; the adapters execute actions addressed to them without gaining access to their private memory. `--once` performs two polling cycles and exits.

For a self-contained, temporary, offline acceptance demonstration, run:

```sh
python -m Ai.universal.demo
```

It starts a real local HTTPS host with random credentials, connects both mocks, completes actions for two agents, rotates an adapter session, verifies an observation, and cleans up.
