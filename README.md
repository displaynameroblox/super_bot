# Super Bot — v2.1.0.alpha.1

Public alpha of the local-first chat server, terminal and browser clients, and
`super-bot-manager`. This is a prerelease; expect bugs and back up client data
before upgrading. Python package version: `2.1.0a1`.

## Tested operating systems

| Operating system | Alpha status |
| --- | --- |
| Linux | Working (maintainer-tested) |
| macOS | Working (maintainer-tested) |
| Windows | Not working; unsupported for this alpha |

Exact distributions, OS versions and hardware have not been recorded here.
The supported-platform CI matrix covers Linux and macOS.

See the [alpha release notes](docs/releases/v2.1.0.alpha.1.md).


## Server administration

The server now has a shared CLI and separate administration dashboard:

```sh
super-bot-manager init --config /path/to/.env
super-bot-manager start
super-bot-manager admin
super-bot-manager update check
super-bot-manager update apply --offline
```

See the [installation, command reference, architecture and recovery guide](docs/server-administration.md)
and [audit and validation report](docs/infrastructure-audit.md). Start works offline;
updates use signed versioned artifacts from GitHub, a local directory or an explicitly
trusted LAN source. Official signed updates require the maintainer to provision the
release signing key and publish the new artifacts; no production signing key is
included in this checkout. Existing configuration and fingerprints can be copied
with `init --config FILE --server-data DIR` after stopping the old server.

## Download and install (AI server and clients)

You need **Python 3.10 or newer**, Git for source installation, and Linux or
macOS. A model provider must be configured separately; installing Super Bot does
not download a model. Local inference keeps model requests local; selecting a
cloud provider sends model context to that provider.

### 1. Download and install

After the alpha is published, download `super_bot-2.1.0a1-py3-none-any.whl`
from [the alpha release page](https://github.com/displaynameroblox/super_bot/releases/tag/v2.1.0.alpha.1).
In Terminal, create an isolated environment and install the downloaded wheel:

```bash
python3 -m venv "$HOME/super-bot-env"
source "$HOME/super-bot-env/bin/activate"
python -m pip install --upgrade pip
python -m pip install "$HOME/Downloads/super_bot-2.1.0a1-py3-none-any.whl"
super-bot-manager --help
```

Alternatively, download and install the tagged source after the tag is published:

```bash
git clone --branch v2.1.0.alpha.1 --depth 1 https://github.com/displaynameroblox/super_bot.git
cd super_bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

For development before publication, clone without `--branch` and install with
`python -m pip install -e .` instead.

In each new Terminal window, activate the same environment before running commands
(`source "$HOME/super-bot-env/bin/activate"`, or `source .venv/bin/activate`
from your source checkout). This also fixes “command not found” when the command
is installed in that environment.

The package installs `super-bot-manager`, `super-bot-server`, `super-bot-web`,
`super-bot-account`, and `super-bot`.

### 2. Start the server

Copy [.env.example](.env.example) to `.env` in the folder you run from and fill in your
model settings. For a local OpenAI-compatible model server already running on
this computer, a minimal `.env` is:

```dotenv
PHONE_MODEL_PROVIDER=phone
PHONE_MODEL_BASE_URL=http://127.0.0.1:8080/v1
TEST_PHONE_API_KEY=your-model-server-key
```

Use the actual endpoint and key for your model server. Wheel users can download
[the configuration example](https://github.com/displaynameroblox/super_bot/blob/v2.1.0.alpha.1/.env.example)
or create `.env` manually. Then run:


```bash
super-bot-server
```

It prints two fingerprints and listens on `https://127.0.0.1:8000` — this computer only.

### 3. Use it on your own computer

```bash
super-bot-web          # browser UI at http://127.0.0.1:8001 (opens automatically)
super-bot-account      # account terminal client
super-bot              # full-screen terminal app (macOS/Linux)
```

The first time a client connects it shows the server's fingerprints; compare them with
what the server printed and answer `y`.

### 4. Use it from other devices on your network

By default the server only accepts connections from the computer it runs on. To share it
with devices on your home or office network:

1. On the server computer, start it with LAN access turned on (this is deliberate and
   prints a firewall warning):
   ```bash
   HOST=0.0.0.0 SUPER_BOT_ALLOW_LAN=1 super-bot-server
   ```
   Only expose it on a trusted network. Account passwords are checked locally;
   server logins are disposable session identities.
2. Allow port `8000` (and UDP `8766` for discovery) through the server's firewall for your
   trusted network only. Find the server's address with `ipconfig getifaddr en0` (macOS),
   `hostname -I` (Linux).
3. On each other device, install Super Bot (step 1) and connect:
   ```bash
   super-bot-web --url https://SERVER_ADDRESS:8000      # or super-bot / super-bot-account
   ```
   Clients on the same network can also find the server automatically if you omit `--url`.
   Check the fingerprints the first time.

The web client is always bound to the computer it runs on; each device runs its own copy
and talks to the shared server. Don't port-forward the server to the internet.

### Publishing a release (maintainers)

Follow the [release checklist](docs/releases/README.md). The release tag is
`v2.1.0.alpha.1`; Python normalizes it to `2.1.0a1`. CI tests Linux and macOS,
builds packages and signed server updates, and publishes a GitHub prerelease.
Publishing requires the pinned public signing key and matching repository secret.

## Getting Started
To build the place from scratch, use:

```bash
rojo build -o "super_bot.rbxlx"
```

Next, open `super_bot.rbxlx` in Roblox Studio and start the Rojo server:

```bash
rojo serve
```

For more help, check out [the Rojo documentation](https://rojo.space/docs).

## AI Terminal App

Install the Python app dependencies with `python -m pip install -r Ai/requirements.txt`.
Run the local model server on a trusted Wi-Fi network as described in
[Ai/termux/README.md](Ai/termux/README.md). In the repository's ignored `.env`
file, set `PHONE_MODEL_BASE_URL` to the phone's address (for example,
`http://192.168.1.42:8080/v1`) and `TEST_PHONE_API_KEY` to the key printed by
the Termux server. Keep the key private. The local phone model uses the stable
API name `superbot-phone`; optional `PHONE_MODEL_NAME` can override it.
`PHONE_MODEL_TIMEOUT_SECONDS` defaults to 600. Then start the session server
with `python -m Ai.server`. By default, it serves HTTPS on `127.0.0.1` only (this computer) at
port `8000`. To let other devices connect, set `HOST` to a LAN address (or `0.0.0.0`)
**and** `SUPER_BOT_ALLOW_LAN=1`; the server refuses to start otherwise and warns
you to check your firewall. LAN-enabled servers advertise themselves with UDP
port `8766`. Set `PORT` or `DISCOVERY_PORT` to override ports.

Before the first start or after changing model settings, run
`python -m Ai.preflight --diagnose`. It migrates and checks the local database,
then verifies the selected model provider and model alias without printing secrets.

#### Client-owned data and temporary server sessions

Accounts, password hashes, profiles, saved chats, knowledge libraries, and opted-in
training examples are stored **on the client computer**. They live in
`client-data/<server-address-hash>/` beside the client's `known_servers.json`.
Accounts belong to that client installation; there is no automatic account/history
sync between devices. Keep a private backup of this directory to move your data.

Real account passwords never leave the client. When needed, the client creates an
opaque temporary server login and sends bounded conversation context and relevant
knowledge passages over the encrypted transport. The server's account/session
store is RAM-only. It forgets the working session and associated login when the
main client disconnects, or the last participant leaves. Unattached sessions expire
after 15 minutes of inactivity. An in-flight model call may finish before its
working context is released. Opening a saved local chat recreates the temporary
server session without generating another greeting.

The server keeps its TLS certificate and encryption identity so clients can verify
it across restarts. Those keys contain no chat history. With Gemini selected,
conversation context is sent to Google's API. Coding CLIs use their own providers;
those providers receive the context required for the requested task.

Old server databases, knowledge files, training files, and backups from previous
versions are **not loaded or deleted automatically**. Preserve or migrate anything
you need before removing those legacy files yourself. Existing terminal chat files
remain on the client; the new local account database starts separately.

Back up a client's account/history database with
`python -m Ai.local_manager create-backup --server-url https://HOST:8000`.
Use `--database PATH` for an explicit database, including a legacy database.
A database-only backup does not include separate knowledge or training files;
copy the entire private client-data directory for a complete backup. Stop clients
before restoring a backup. A recovery code resets a local password; there is no
email recovery service.

Messages are TLS-protected and sealed to the server's key in transit. The server
and selected model provider must read working context to answer.

#### Web client (runs on the client side)

`python -m Ai.web_client --url https://HOST:8000` starts a browser UI on **your own
computer** (default `http://127.0.0.1:8001`, `--port` to change, `--no-browser` to skip
opening it). The server can be on another machine: the web client forwards the browser's
calls over the same TLS + end-to-end sealed, fingerprint-verified transport as the terminal
clients, and shares their trusted-servers list. The server itself serves no web pages.
The page is bound to this computer only. You can create an account, sign in, recover a
password, chat, search and delete chats, and manage your profile, password, recovery code
and sessions. Browsers never receive API keys or chat-session tokens; the account token is
kept in an HttpOnly, SameSite=Strict cookie, with a strict Content-Security-Policy,
Host and Origin checks, and a required request header against cross-site requests. Development startup tests are opt-in with `SUPER_BOT_RUN_TESTS=1`.

#### Full-screen terminal interface

Run `super-bot` (or `python -m Ai.chat_app`). On the welcome screen, press Enter
to create a conversation, or enter a session ID to join one. The interface uses
your terminal's background, with color accents and a monochrome fallback.
Wide terminals show a clickable conversation sidebar; smaller terminals keep
the chat and composer visible. The minimum size is 40 columns by 12 rows;
80 by 24 or larger gives messages more room.

| Shortcut | Action |
| --- | --- |
| Ctrl+P | Search actions and commands; selecting a command puts it in the composer for review |
| Ctrl+O | Search and open saved conversations |
| Tab / Shift+Tab | Switch views; Tab after `/` searches commands |
| F2–F6 | Open Chat, Data, Activity, Coding, or Review |
| F7 | Toggle chat/coding split view (side by side at 90+ columns) |
| Left / Right, Home / End | Move through your draft; with an empty draft, navigate views or scroll |
| Ctrl+U / Ctrl+W | Clear the draft / delete the previous word |
| Up / Down, Page Up / Page Down | Scroll the current view; mouse wheel also works on supported terminals |
| Ctrl+A / Ctrl+D in Review | Apply / discard the reviewed patch |
| Esc | Close an overlay or return to Chat |
| F1 | Show or hide extra keyboard hints |
| Ctrl+C | Exit, including while a reply is pending |

The composer keeps long drafts and the cursor visible, shows message length,
and lets you draft your next message while a response is pending. Code blocks
keep their indentation. Patch review starts at the beginning and distinguishes
added and removed lines by both color and `+`/`-` markers. Use `/help` for the
complete command guide. This interface uses the same client-owned storage and
temporary server sessions described above.

#### Account terminal client

`python -m Ai.account_app` is a Python client that signs in to a server account over the
same encrypted, fingerprint-verified transport as `chat_app` (sharing its trusted-servers
list). Register, log in or recover a password, then `/new`, `/chats [search]`, `/open ID`,
`/delete ID`, `/profile`, `/knowledge`, `/password`, `/recovery`, `/logout`, `/logout-all`,
`/delete-account`. `/knowledge add FILE [CATEGORY]` indexes UTF-8 text from the local
computer into the signed-in account's private library on that same computer. Chats are resumable on the client that saved them, and, like
`chat_app`, it runs the tests first (`--skip-tests` to bypass).

Not yet in this release: provider configuration inside the curses client, optional Roblox bridge and installer.

When starting the secure server from the Termux menu, choose whether to use the
phone's local model or Google Gemini. The phone model is the default and needs
`PHONE_MODEL_BASE_URL` and `TEST_PHONE_API_KEY`. Gemini needs a
`GEMINI_API_KEY`; the Termux prompt hides the key and keeps it in memory only
for that server run. You can set `PHONE_MODEL_PROVIDER=gemini` and
`GEMINI_API_KEY` in the server environment or ignored `.env` file when starting
`python -m Ai.server` directly. `GEMINI_MODEL_NAME` optionally selects a model
(default `gemini-3.8-flash`). When Gemini is selected, conversation text is sent
to Google's API; the startup banner identifies the selected provider.

To use OpenAI cloud inference, add these settings to the server's ignored `.env`
file (or set them in its environment), then restart the server:

```dotenv
PHONE_MODEL_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL_NAME=your-openai-model-id
```

Choose a model available to your API key that supports Chat Completions and JSON
schema responses. The server sends conversation text and included context to
OpenAI over HTTPS; the API key stays on the server. Startup checks model access,
and chat errors do not expose the key or upstream response body. The timeout uses
`PHONE_MODEL_TIMEOUT_SECONDS` (600 seconds by default). The Termux secure-server
menu also offers OpenAI with a hidden API-key prompt and a model-ID prompt.
An explicit cloud provider takes precedence over a saved local GGUF selection.
Managed startup reads the launch folder’s `.env`, then the instance `.env`;
explicit environment variables take precedence over both.

Start a client with `python -m Ai.chat_app`. It discovers servers on the local
network; if none are found, it asks for a server address. Use `/connect` to
choose another server, `/new` to create a session, and `/login SESSION_ID` to
join one. A session can be password-protected when it is created. Working session data
is held temporarily in server memory and is removed when its host disconnects.
Saved history stays on the client. Passwords are
stored as salted verifiers, while login tokens are saved in the client's local
chat file. The session server sends recent conversation history and the
system instruction to the selected model; each response includes both the chat
reply and coding-request classification. Coding briefs, classification,
refinement, and command proposals use the same selected model. Proposed commands
still require explicit approval, and coding-agent patches still require review.
Conversation context is bounded; response time depends on the phone and network.
Use `/account login` to sign in to a server account in the full-screen client.
Its **Data** tab lists private account knowledge; `/knowledge add FILE [CATEGORY]`
indexes bounded UTF-8 text locally, and `/knowledge delete ID` removes it.
Account tokens are memory-only in this client and are never added to local profile
or session files.

If a coding CLI is missing, `/agent install NAME` (for example `/agent install claude`)
shows the exact install commands and runs them only after you confirm with `y`.
Installs use fixed commands for Copilot, Claude Code, Codex, Gemini, OpenCode,
Amp (via `npm`), and Aider (via `pipx`); a missing `npm`/`pipx` is installed first
through Homebrew (or `pkg` in Termux) when available. Cursor Agent has no
automatic installer, so the command shows its official page instead.
`git` is also required for the agent's isolated workspace and is not installed by the app.

Copilot runs with explicit file read/write permissions in a disposable project
copy. Shell commands are denied by default; use `/command REQUEST`, review the
proposal, and `/approve` to run one in the real workspace. The copy is not an OS
security sandbox. Other coding providers retain their own permission controls.
Old built-in CLI templates are upgraded automatically; custom templates are kept.
Review the resulting patch with `/diff`, then `/apply` or `/discard`.
The Coding tab (F5) shows CLI output; quota and login failures also appear in the
status line. A missing quota cannot be repaired by this app: restore provider
access or select another installed CLI. See [Copilot tool permissions](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools).


Set the AI's reply style with `/style`: pick a preset (`casual`, `formal`, `playful`,
`friendly`, `technical`, `concise`) or write your own, for example
`/style dry humour, one sentence max`. `/style off` clears it. You're also asked for a
style at `/new`. The style is stored on the session server, applies to everyone in that
session, and is separate from the persona, so changing it never rewrites the persona.

Chat skills change how the AI behaves in conversation: `roleplay` (in-character replies
with `*actions*`, which the client already highlights), `storyteller`, `copilot-brief`
(helps you prepare a request for a coding agent), and `tutor`. `/new` asks which skills
to use (up to three, comma separated), and the client sends them with the new session.
Write your own with `/chatskill create NAME INSTRUCTIONS` (500 characters max); they are
saved on your computer in `chat_skills.json` next to your chats, and `/chatskill delete
NAME` removes one. The session server never stores a skill library or user data on disk: it only keeps the skills you sent in memory for the life of that session, exactly
like the chat log. Each saved session remembers its skills locally. The session host can
change them mid-session with `/chatskill use NAME`, `off NAME` or `clear`, and other
participants receive them when they log in. Host status belongs to the login token issued
when the session was created, so a host who logs in again with the password becomes an
ordinary participant. Chat skills are separate from `/skill`, which is for the coding
agent.

UDP discovery only works on a reachable local subnet. For a server on another
network, enter its reachable address manually; routers, firewalls, VPNs, and
hotel/hotspot client isolation determine whether devices can connect. Client
connections are always HTTPS and end-to-end encrypted; see
[Secure connections](#secure-connections-https-and-end-to-end-encryption).

### Secure connections: HTTPS and end-to-end encryption

The session server only speaks HTTPS. There is no plain-HTTP mode, and the
client refuses `http://` addresses (a bare address such as `192.168.1.20:8000`
means `https://192.168.1.20:8000`). On top of TLS, every request, response,
session password, login token, and live event between a client and the server
is end-to-end encrypted: the client seals it to the server's pinned key
(X25519, HKDF-SHA256, ChaCha20-Poly1305) and only the Super Bot server process
can open it. A proxy, tunnel, or Wi-Fi observer in between, even one that
terminates TLS, sees only ciphertext and cannot tell which endpoint is being
called. The session server and selected model provider (phone-hosted or Google
Gemini) see your messages in plain text, because the AI endpoint must read them
to answer.

**First start.** `python -m Ai.server` creates a self-signed TLS certificate and
an encryption key in a private directory and prints their fingerprints. They are
reused on later starts. The directory is
`~/Library/Application Support/super_bot/server` on macOS,
`$XDG_DATA_HOME/super_bot/server` (default `~/.local/share/...`) on Linux, and
`%APPDATA%\super_bot\server` on Windows; set `SUPER_BOT_SERVER_DIR` to change
it. Keep that directory private and out of the repository. To use your own
certificate, set `SSL_CERTFILE` and `SSL_KEYFILE` (plus `SSL_KEYFILE_PASSWORD`
for an encrypted key).

**First connect.** The first time a client connects to a server, it shows those
same fingerprints and asks you to compare them with the ones the server printed,
like SSH host keys. Answer `y` and they are saved in `known_servers.json` next
to your chats; every later connection must match. If a fingerprint changes, the
client refuses to connect: either the server's keys were reset or someone is
intercepting the connection. `/server` shows the verified fingerprints, and
`/server forget` clears them so `/connect` can verify the server again. A
server whose certificate comes from a trusted authority needs no certificate
pin; for a private authority, set `SUPER_BOT_CA_CERT` to its PEM file.

| Setting | Purpose |
|---------|---------|
| `SSL_CERTFILE`, `SSL_KEYFILE`, `SSL_KEYFILE_PASSWORD` | Server: use your own certificate instead of the generated one |
| `SUPER_BOT_SERVER_DIR` | Server: where the generated certificate and encryption key live |
| `SUPER_BOT_CA_CERT` | Client: extra certificate authority to trust for the server's certificate |
| `PHONE_MODEL_PROVIDER` | Server: `phone` (default), `gemini`, or `openai` |
| `OPENAI_API_KEY`, `OPENAI_MODEL_NAME` | Server: OpenAI API key and model ID (both required for OpenAI) |
| `GEMINI_API_KEY`, `GEMINI_MODEL_NAME` | Server: Gemini credentials and optional model name |
| `PHONE_MODEL_CA_CERT` | Server: certificate to trust for an `https://` phone model link |

### Local training, inference, and knowledge updates

The local training notebook is [Ai/local_train_superbot.ipynb](Ai/local_train_superbot.ipynb).
It fetches capped, streamed samples from the PDF's OpenAssistant, WildChat,
UltraChat, SmolTalk, No Robots, Dolly, CodeAlpaca, and Evol-Instruct-Code links,
then fine-tunes Gemma 2 2B using Transformers/PEFT LoRA. The notebook supports
CUDA, Apple MPS, and CPU depending on the host. This alpha supports macOS and Linux; a GPU is recommended,
while CPU training may be very slow and needs substantial free RAM. The Stack
and Stack Exchange are not blindly downloaded as training data; use reviewed,
license-filtered excerpts as knowledge sources. Check the current terms and
licenses of every dataset before training. The resulting adapter uses the
standard PEFT format. It can be loaded by a Transformers-compatible inference
server; convert or merge it before using an MLX or llama.cpp/GGUF backend.
After training, the notebook can upload the LoRA adapter to a private Hugging
Face model repository. Set `SUPER_BOT_HF_REPO_ID` and authenticate with your
Hugging Face account. New repositories are private by default; an existing
repository requires typing `UPDATE` before a new commit is uploaded. Public
repositories are refused. The upload is the adapter, not a merged standalone
model.

User chats can be included in a later training run with explicit opt-in. Collection
is off by default. `/learn on` saves future exchanges **on that client only**;
`/learn off` stops collection. The server never saves training examples, even if
the old `SUPER_BOT_COLLECT_CHAT_TRAINING` setting is present. Files are under
`client-data/<server-address-hash>/training/<local-account-id-or-guest>/`.
Export with `python -m Ai.local_manager export-chats --database PATH_TO_CHAT_EXCHANGES_SQLITE`,
then review the JSONL before training. This queues examples for a later batch
fine-tune; it does not change model weights live.

The local manager provides local model and client-data operations without adding model
administration endpoints to the public encrypted chat API. Training is
optional: the server can download and use an already-published model with
`download` and `serve` alone. Run the notebook's
setup and data-preparation cells first; they install training dependencies and
write `train.jsonl` and `valid.jsonl` for the manager's optional `train`
command:

```sh
# Train from train.jsonl and valid.jsonl in that directory (CUDA, MPS, or CPU).
python -m Ai.local_manager train \
  --data-dir "$HOME/Library/Application Support/super_bot/training-data"

# Export consented client-side chats for the notebook to include.
python -m Ai.local_manager export-chats --database PATH_TO_CHAT_EXCHANGES_SQLITE

# Download a published model without training anything. With --filename it
# fetches one file (e.g. a GGUF); without it, the whole repository (e.g. your
# uploaded adapter; private repos need `huggingface-cli login`).
python -m Ai.local_manager download --repo OWNER/REPO --filename MODEL-Q4_K_M.gguf
python -m Ai.local_manager download --repo OWNER/REPO

# Download and serve the published Super Bot Chatter LoRA adapter on macOS.
# This needs MLX and access to the Gemma base model.
python -m Ai.local_manager download-superbot
python -m Ai.local_manager serve-superbot

# Index into one account's private knowledge library. Account IDs are shown in
# the browser Account API and terminal account client.
python -m Ai.local_manager index \
  --url https://docs.python.org/3/library/sqlite3.html \
  --category programming \
  --account-id 1

# Run the downloaded GGUF locally with llama.cpp.
python -m Ai.local_manager serve --backend llama.cpp \
  --model-path "$HOME/Library/Application Support/super_bot/models/MODEL-Q4_K_M.gguf"

```

Keep the inference service bound to `127.0.0.1`. Point the session server at
its OpenAI-compatible endpoint in `.env` with
`PHONE_MODEL_BASE_URL=http://127.0.0.1:8080/v1` and
`TEST_PHONE_API_KEY=local-superbot`, then start `python -m Ai.server`. This
preserves the existing encrypted client-to-session-server connection.

Knowledge is stored separately from model weights and training data in local
SQLite/FTS5 databases. Use `--account-id` to index into an account's private
library; the browser client lists and removes only that account's sources.
Without it, the manager uses the legacy shared library. Add or refresh one
carefully selected source at a time; indexing the same source URL again
replaces its old passages:

```sh
python -m Ai.local_manager index --url https://docs.python.org/3/library/sqlite3.html \
  --category programming --title "Python sqlite3 documentation" --license PSF
python -m Ai.local_manager index --file ./reference.pdf \
  --category history --title "Reference document" --license "check source terms"
```

Supported sources are text, HTML, and text-extractable PDFs up to 20 MiB.
Indexed passages retain title, source URL/path, category, author/date/license
metadata, and are retrieved with FTS5/BM25 during chat; answers are instructed
to cite source URLs. The database is local and continues to work offline.

Things to know:

- The end-to-end layer has no forward secrecy against theft of the server's
  encryption key (TLS still provides it on the wire), so protect that directory.
- Requests carry a timestamp for replay protection, so client and server clocks
  must agree within about five minutes.
- Only `/health`, the server's public identity, and the encrypted endpoints are
  reachable. The plain REST API, `/docs`, and `curl` access to it no longer
  exist, so the Roblox script in `src/server/modules/ai_handler.luau` (plain
  HTTP) needs a bridge that speaks this protocol.
- With the phone model selected, the link from the session server to the phone
  is separate from client connections. It stays plain HTTP unless you opt in
  (see [Ai/termux/README.md](Ai/termux/README.md)), and the server prints a
  warning at startup while it is unencrypted. Gemini mode instead sends chat
  content directly to Google over HTTPS.

### AI folder layout

| Path | Purpose |
|------|---------|
| `Ai/server.py` | Session server (FastAPI) that routes chat to the selected model provider |
| `Ai/gateway.py` | The only network-facing app: `/health`, the server identity, and the encrypted endpoints |
| `Ai/e2ee.py`, `Ai/tls.py` | End-to-end encryption protocol; server TLS certificate handling |
| `Ai/transport.py` | Client side: trusted-server store, pinned HTTPS, encrypted requests and events |
| `Ai/phone_model.py` | Provider client for the phone model or Google Gemini |
| `Ai/local_manager.py`, `Ai/knowledge.py` | Local model operations and SQLite/FTS5 knowledge retrieval |
| `Ai/training_data.py`, `Ai/chat_training.py` | PDF-linked SFT dataset normalization and opt-in local chat exports |
| `Ai/local_train_superbot.ipynb` | Cross-platform Transformers/PEFT LoRA training notebook |
| `Ai/chat_app.py`, `Ai/coding_tools.py` | Terminal client and coding-agent tools |
| `Ai/protocol.py` | Discovery constants shared by server and client |
| `Ai/termux/` | Phone-side model manager and Mac helper script |
| `Ai/tests/` | Test suite: `python -m pytest Ai` from the repository root |
#### Development tests

Run `python -m pytest Ai -q` from a development checkout with pytest installed.
Startup tests are opt-in with `SUPER_BOT_RUN_TESTS=1`; ordinary client starts do
not run the suite.
