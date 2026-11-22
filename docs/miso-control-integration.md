# Miso Control integration specification

For account linking and memory recovery, see [Shared Miso memory](miso-memory.md).

Miso Control source was unavailable on the inspected Mac. This change supplies
the memory endpoints for it, a future Android client, trusted Minecraft SDK
clients and other authorized Super-Bot clients. Each installation keeps the
SQLite database on the authority host. Remote devices use the Super-Bot
encrypted request transport with independent device bearer credentials.

## Implemented interface

Run the updated Super-Bot server on the authority machine with
`SUPER_BOT_MEMORY_API_DB` explicitly set to the enabled TUI `client.db`.
The endpoints are disabled by default. Use the existing Super-Bot sealed
request protocol over HTTPS. Verify both the TLS and encrypted transport
identity fingerprints out of band. The Python reference implementation is
`Ai.transport.secure_request`, with envelope handling in `Ai.e2ee` and
the route allowlist in `Ai.gateway`. Use the same wire protocol for other
languages. Send device authorization only inside the sealed request.

| Method and route | Scope | JSON |
| --- | --- | --- |
| POST `/miso/search` | `read` or `read-write` | `{"query":"project name"}` |
| POST `/miso/remember` | `read-write` | `{"request_id":"stable-client-generated-id","text":"A personal note"}` |

Search returns up to four passages with source, thread ID, timestamp (nullable),
prompt and reply. The backend derives the profile from the credential. Notes
use a stable 16–100 character request ID consisting of letters, digits,
underscores or hyphens. The same request ID in the same profile is idempotent.
A revoked, expired, wrong-scope or unrelated bearer gets HTTP 403. When the
API is disabled it returns HTTP 404. Render returned text as untrusted data.

## Provision and revoke devices

On the authority host, in a private interactive terminal:

```sh
python -m Ai.miso_admin --db "$MISO_DB" issue-device YOUR_LOCAL_USERNAME "Miso Control Linux" --scope read
python -m Ai.miso_admin --db "$MISO_DB" list-devices YOUR_LOCAL_USERNAME
python -m Ai.miso_admin --db "$MISO_DB" revoke-device YOUR_LOCAL_USERNAME DEVICE_ID
python -m Ai.miso_admin --db "$MISO_DB" revoke-all-devices YOUR_LOCAL_USERNAME
```

The first command requires the local account password and displays the
credential once. Copy it directly to a per-device credential store. Use a
desktop secret service or equivalent in Linux, and encrypted app storage with
a key protected by Android Keystore on Android. Keep tokens out of logs,
configuration exports, clipboard history and crash reports. A device token
expires after 30 days. Revoke and reissue a lost credential from the authority
host. Use bulk revocation if a device or backup is compromised. A restored
snapshot may restore older credentials, so revoke and reprovision after recovery.
Local account logout and device revocation are distinct operations.

## Future integration

- A chat endpoint should accept a private thread ID and text and create a scoped
  saved exchange on the authority. The client must receive a chat-specific
  device grant. The current memory API exposes search and notes.
- Treat node inventory, health, active provider and model as a separate read-only
  management API. Use independent, device-bound management grants. ICARUS and a
  Linux workstation can eventually register and emit authenticated heartbeat
  data. None of the memory grants authorize node discovery or management.
- Add start and stop only after a management authority is implemented and
  verified. Use allowlisted service IDs, narrowly scoped per-device grants,
  expiring one-use action challenges and explicit confirmation in a trusted
  management UI. Bind the confirmation to actor, node, service and action.
  Keep an audit record of identifiers and outcomes without credentials or
  conversational content.
- Model output, Discord messages, retrieved memory and Minecraft chat are
  untrusted inputs to any management interface. Keep management credentials
  outside model context. A trusted operator interface must initiate and
  authorize actions directly. The memory API has no command, SSH or service
  operation capability.
