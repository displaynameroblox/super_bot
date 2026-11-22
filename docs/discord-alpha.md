# Miso Discord alpha

Install Super-Bot with the `discord` extra. For a checkout, run `python -m pip install -e '.[discord]'`.

For a standalone bot, use the local setup below. For the managed gateway, set `SUPER_BOT_DISCORD_ENV` in the instance environment and start it with `super-bot-manager start`. For linked TUI/Discord memory, follow the [shared node setup](miso-3.1.0a1.md#discord-and-shared-memory-setup). Check gateway readiness with `super-bot-manager status` and startup failures in the instance's `discord.log`.

## Set up Discord

1. In the Discord Developer Portal, open the Miso application and its **Bot**
   settings. Keep the token local. For a closed alpha you can disable **Public Bot**.
2. If the bot is already in your test server, check its role and channel
   permissions. For a new invite use the `bot` OAuth scope and grant **View
   Channel** and **Send Messages** in the intended text channels. Ordinary bot
   messages work with these permissions. Leave administrator, member and presence
   intents, command scopes, and Discord's **Read Message History** permission off.
3. Enable Developer Mode in Discord and copy your user ID and the IDs of the
   test text channels. Use your user ID, not the bot or application ID.
4. From the repository, create the private file with
   `(umask 077; set -C; cat .discord.env.example > .discord.env)`. Fill in the token
   and IDs in a local editor. Keep the template empty of credentials.
   Environment variables override file values. `--env-file` selects a different
   owner-only file. On POSIX the launcher rejects files readable by other users.
   On Windows, restrict the file ACL to your account.

Leave `DISCORD_CHANNEL_IDS` empty for DM-only operation. Otherwise, only the
listed text channels are eligible. The default server mode responds to direct
bot mentions and works with **Message Content Intent** disabled. To respond to
all text in configured channels, set `DISCORD_MENTION_ONLY=0` and enable that
intent in the Developer Portal. One-to-one DMs work in both modes. Group DMs,
threads, webhooks, other bots, attachments and slash commands are outside this
alpha. See the [discord.py intent reference](https://discordpy.readthedocs.io/en/stable/api.html#discord.Intents.message_content).

## Choose inference at startup

Run `super-bot-discord` in a terminal and pick OpenAI, Gemini, OpenRouter or
local. Use `--provider` or `DISCORD_PROVIDER` for unattended runs. Configure the
selected provider in `.discord.env`, your environment, or the existing
Super-Bot `.env`.

| Provider | Configuration |
| --- | --- |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL_NAME` |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL_NAME` if overriding Super-Bot's default |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL_NAME` (the full model ID) |
| Local | An existing loopback endpoint using `PHONE_MODEL_BASE_URL`, `TEST_PHONE_API_KEY` and `PHONE_MODEL_NAME`, or a Super-Bot managed instance |

An existing endpoint must use a loopback IP such as `http://127.0.0.1:8080/v1`.
For a GGUF already selected in Super-Bot's model manager, install
`llama-server` and run
`super-bot-discord --provider local --managed-root "$HOME/Library/Application Support/super_bot/instances/default"`
on macOS. This starts the managed model for the lifetime of the bot. Cloud
selection does not start the runner. OpenRouter checks the key at startup and
selects upstream routes that accept the request parameters. Choose a specific
text model. [OpenRouter API reference](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)

## Import Miso and try the alpha

On first startup, Super-Bot copies the saved `Miso` TUI persona, player name,
reply style, active skills and enabled character notes into the owner's DM
profile. The startup model selection controls inference. The existing preferred
model is saved as profile metadata. The default profile source is
`chat_profiles.json` next to the standard TUI history file. For a custom TUI
location, pass `--profiles-file`. Use `--profile-name` if the saved name differs.

To refresh the profile offline, use `super-bot-discord --import-only`. Add
`--copy-profile-history` to copy the saved profile transcript. Add
`--copy-knowledge /path/to/owner-knowledge.db` to copy passages from a specific
Super-Bot knowledge library you own. Review the source before choosing it.
The import reads the selected sources and writes only to the Discord store.
Repeating an import refreshes the selected snapshot without appending duplicate
turns. New Discord exchanges stay in the Discord database. The full transcript
copy is stored locally and up to 2,500 bytes are included in an owner DM request.
The main TUI conversation list and training data are outside this import.

DM Miso as the configured owner, then try a second Discord account. Only the
owner DM should recognize imported details. The second account should remember
its own DM exchanges and get the default cuddly persona. Mention Miso in two
allowlisted channels to verify each channel has its own shared context. With the
default settings, an ordinary unmentioned channel message gets no response.

The SQLite database lives in the `discord` subdirectory of Super-Bot's app data
directory. Use `--data-dir` to select a dedicated location. The database is
bound to the configured owner ID. Keep a private backup if you need it. Each
inference request sends bounded conversation context to the selected model.
Owner DM context, including any copied material used in that turn, is therefore
sent to the selected cloud service when you use a cloud model.

## Limits

The defaults are one in-flight exchange per bot process, one inference request
per user every 10 seconds, and 60 attempts per UTC day across all users.
Cooldown and daily counters survive restarts. Each request caps current text
at 1,800 UTF-8 bytes, includes a bounded slice of history, and requests at most
512 output tokens. Provider failures consume an attempt. Messages arriving
while busy or over a limit are skipped. Set `DISCORD_DAILY_REQUESTS` and
`DISCORD_USER_COOLDOWN` to change those limits. Use a dedicated provider key with
a provider-side spending cap where available if you need a currency limit.
