# Shared Miso memory

For platform setup, see the [Miso integration guide](miso-3.1.0a1.md). For a standalone Discord bot, see [Discord setup](discord-alpha.md).

The TUI already stores chats and local account credentials in a per-server
`client-data/<server-hash>/client.db`. Its selected character, style, skills
and character notes are saved in `chat_profiles.json`. The Super-Bot inference
server's account and session state is temporary. The Discord alpha used
`discord/discord.sqlite3` for profiles, private DMs and server channels. The
shared memory service extends the existing TUI database. Each client reads fresh
committed rows before generating a reply.

On the inspected Mac, the TUI database is at
`~/Library/Application Support/super_bot/client-data/03f6d7ba09b0531a17805965/client.db`.
Before this change it contained 24 ownerless chats, 24 structured transcripts,
107 exchange markers and zero local accounts. One chat's saved persona begins
with the current Miso persona. That match helps locate a candidate for review.
It provides no proof of ownership. The older managed
`instances/default/data/sbp/sbp.sqlite3` contains the `audit` and `objects`
tables. It is outside this migration. The separate TUI JSON session list and
profile snapshots stay in place.

## Enable and link

Use the same Super-Bot checkout for the TUI and Discord. Update the inference
server too. It validates that memory context is used only in an authenticated
private session. The next TUI launch must use the same server URL as the
existing database. The local accounts are specific to that database.

From the checkout, set `MISO_DB` to the existing TUI database. These commands
use the project's Python environment. Pick a username and a password locally.

```sh
MISO_DB="$HOME/Library/Application Support/super_bot/client-data/03f6d7ba09b0531a17805965/client.db"
python -m Ai.miso_admin --db "$MISO_DB" enable
python -m Ai.miso_admin --db "$MISO_DB" register YOUR_LOCAL_USERNAME
```

1. Add `SUPER_BOT_MEMORY_DB` with that absolute path to the bot's private
   `.discord.env`. Use quotes around the path. Restart the Discord bot.
2. In the TUI, use `/account login`. Select your saved Miso profile with
   `/profile use Miso`.
3. Run `/account link-discord YOUR_DISCORD_USER_ID`. Send the displayed
   `/link CODE` in your one-to-one Miso Discord DM within 10 minutes. The code
   is single-use and requires the issuing account login to remain valid. The
   bot checks the numeric sender ID from Discord. A name match has no role.
4. Start a new TUI chat while signed in and using Miso. Older guest sessions
   keep their existing ownership. If the TUI says “Guest chat” after a reply,
   switch to an account chat. Signing in leaves the open guest chat separate.
   Restate any correction you intend to share in an account chat or in the
   linked Discord DM. To refresh Discord's profile snapshot after
   changing TUI character settings, repeat the link flow from the same account.
   To revoke it, use `/account unlink-discord`.

With shared memory enabled, the configured Discord owner gets the default
persona until the link is confirmed. Guest DMs get separate internal profiles.
The bot keeps channel history and rate-limit counters in the existing Discord
store. New private Discord exchanges write to the shared TUI store. The TUI and
Discord keep their own chat views and can recall information from each other.

## Legacy imports and recovery

Enabling memory takes a verified SQLite snapshot via the backup API before
adding the new tables. Each import also snapshots the destination. The legacy
Discord database is opened read-only. No source chat is rewritten. Keep the
originals and the copies under the TUI database's private `backups` directory.

After linking, import the legacy Discord owner's DMs, any previously imported
profile transcript and existing owner knowledge passages:

```sh
python -m Ai.miso_admin --db "$MISO_DB" import-discord YOUR_LOCAL_USERNAME \
  "$HOME/Library/Application Support/super_bot/discord/discord.sqlite3"
```

Review the old Miso sessions, then supply only their numeric session IDs.
Use `--adopt-unowned` only for an explicitly selected guest-era chat you own
and intended to share with your linked private profile.

```sh
python -m Ai.miso_admin --db "$MISO_DB" import-tui YOUR_LOCAL_USERNAME SESSION_ID --adopt-unowned
```

The commands report validated and newly imported record counts. A second run
against the same snapshot reports zero new records. A source identity mismatch,
existing claim by another profile or changed source record aborts the entire
import transaction. Legacy TUI import preserves session IDs, structured message
order and roles, the full transcript, and its recorded session creation time.
Per-message timestamps remain in the original transcript when available.
The legacy Discord history table has ordered row IDs but has neither timestamps
nor Discord message IDs. Its imported rows keep their source row IDs and an
unknown timestamp. The request ledger cannot reliably supply timestamps for
those rows.

To back out of the feature, unlink Discord in the TUI, set
`SUPER_BOT_MISO_MEMORY=0` in the TUI process, clear `SUPER_BOT_MEMORY_DB` and
`SUPER_BOT_MEMORY_API_DB` in the other processes, and restart them. This approach preserves new memory records. For a database restore,
first stop the TUI, Discord, and any memory API process touching the file.
Keep a current snapshot, then use Super-Bot's verified
`Ai.backup.restore_backup(backup_path, db_path)` with the pre-change snapshot.
Restoring an old snapshot rewinds later writes. Rotate any tokens that may have
been valid in the restored snapshot.

## Storage and isolation

- `miso_profiles` uses a stable random profile ID, with unique account and
  Discord identities. Only the matching DM sender can complete a pending link.
  Existing guest memory is preserved separately during linking.
- `miso_links` stores a digest, target Discord identity, issuing account token
  reference and expiry. The raw code is shown in the TUI and handled as a
  command before inference or transcript persistence.
- `miso_turns` stores completed exchanges and authorized notes with profile
  ownership, source, thread, external identifier, text and time. A unique source
  key prevents replay duplicates. `miso_imports` records legacy ownership claims.
- `miso_devices` holds per-device hashed credentials and scopes. It has no
  administrative permissions.
- SQLite uses WAL, a 15-second busy timeout and short transactions. The TUI
  commits its transcript and memory in one transaction. Discord rechecks the
  linked identity when storing its reply. The profile predicate in each
  retrieval query limits the rows before ranking term matches and recent turns.
  Relevant explicit user updates (for example, “Correction: …” or “Remember
  this: …”) get priority. Shared recall quotes user text only and orders it
  newest first. An explicit update to a simple attribute suppresses older
  statements about that attribute in shared recall. The model is instructed
  to follow the newest applicable correction, including corrections in the
  current message. Assistant replies remain in each client's own history.
- Private memory excerpts go to the inference provider selected by each client.
  TLS and the encrypted Super-Bot transport protect them in transit to the
  server. Cloud inference processes the supplied excerpts. The local model can
  process these excerpts on the machine hosting inference.

## Verify the setup

1. Tell Miso an invented fact in an owner Discord DM, such as “My test project
   is called Silver Orchard.” After a reply, ask for the project name in a
   fresh private TUI chat.
2. Tell Miso a different invented fact in that private TUI chat. After a reply,
   ask Miso for it in the owner DM. Restart each client and repeat the recall.
3. Ask for both facts in a guest DM and in an allowlisted server channel.
   The guest and server should have only their own contexts and the default
   persona. Leave time for the Discord cooldown between messages from the same
   account. Use an available provider. A failed inference does not create a
   completed exchange.

Automated tests exercise these paths with generated test data and mocked
inference, including real FastAPI/TUI request handling and Discord routing.


Set `SUPER_BOT_MEMORY_DIAGNOSTICS=1` in the TUI and in the process handling
Discord inference (the bot in direct mode or the node in node mode).
Compare the `profile` fingerprint and `rows` (internal memory ID and
lexical score) for the same question on both clients. `included=True` means
the memory block was attached to that client's inference request.
`reason=guest-chat` identifies a TUI chat that was opened without an account.
Diagnostics are off by default and omit chat content, account identifiers,
tokens and keys.
