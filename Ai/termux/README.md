# Shared server management

For a fresh wheel install, follow the [binary-only Termux setup](../../docs/server-administration.md#termux). It uses prebuilt dependencies and exits if a compatible wheel is unavailable.

Prefer the [shared Python manager](../../docs/server-administration.md) for server
start/stop, signed updates, rollback and administration. The model menu below is
retained for local inference. When the manager is installed, secure-server actions
delegate to it. Startup is offline and local-only by default. Set `HOST` and
`SUPER_BOT_ALLOW_LAN=1` explicitly to share the server. The legacy updater requires
an explicit `MAC_SERVER_URL`; there is no built-in developer address.

# Manage local models in Termux

The phone runs `llama.cpp` and exposes one OpenAI-compatible API on trusted
Wi-Fi. The model manager lets you download, select, and remove GGUF models on
the phone without editing scripts. Super Bot always calls the server under
the stable model name `superbot-phone`, regardless of which GGUF is selected.

## Install or update the phone manager

On the Mac, run `bash Ai/termux/serve-from-mac.sh` from the checkout.
The publisher creates a persistent signing key at
`~/.config/superbot/release-private.pem` on first use. Existing installations
must continue using their original signing key, passed as the optional argument.

On the phone, download the launcher, updater, requirements, and public key
over a trusted connection. Set the Mac address first.

```sh
export MAC_SERVER_URL=http://YOUR_MAC_IP:8765
mkdir -p "$HOME/superbot-launcher"
cd "$HOME/superbot-launcher"
for file in start-server.sh update-server.py requirements.txt release-public.pem; do
    curl --fail --location "$MAC_SERVER_URL/$file" -o "$file" || break
done
bash start-server.sh
```

Choose **7** for the first signed installation or an update, then **1** for
the local phone model. Once installed, choose **6** for normal startup.
Normal startup uses the installed release and local model without contacting
the Mac. Keep the downloaded files together. The Mac publisher can be stopped
after the update completes. New model downloads and missing dependency
installation still require internet access.

The model files remain in `~/models`, and the selected model and API key remain
in `~/.config/superbot`.

## Use the model manager

Running `bash start-server.sh` opens a menu:

1. List installed `.gguf` models and show the selected model.
2. Download Qwen3 4B Q4_K_M (~2.5 GB), Qwen3 8B Q4_K_M (~5.0 GB),
   Gemma 1.1 7B instruct Q4_K_M (~5.3 GB), Gemma 2 9B instruct Q4_K_M
   (~5.8 GB), another Hugging Face GGUF, or Super Bot Chatter
   (option 6; ~5.2 GB). The 7B/9B models
   need roughly 7+ GB of free RAM; try Qwen3 4B first if your phone struggles.
   Gemma downloads come from the ungated `bartowski` repositories.
3. Select the model to launch.
4. Delete an inactive model after confirmation.
5. Start the API server.

For a future/community model, choose the custom download option and enter its
Hugging Face repository (for example `owner/model-repo`) and exact `.gguf`
filename for a **single-file** model. It must be an **instruct/chat GGUF** with
a chat template supported by the installed `llama.cpp`; split/sharded GGUFs
are not downloaded as a group. For gated or private Hugging Face repositories,
set `HF_TOKEN` in Termux before starting the manager. Do not share that token.
The manager resumes interrupted downloads and validates the GGUF file header
before adding it. For custom downloads it shows available storage and asks for
confirmation because the file size is not known in advance.

Option 6 downloads
[Super Bot Chatter](https://huggingface.co/Displaynamethecat/super-bot_chatter-0.1)
into `~/models` (about 5.2 GB). The download resumes if interrupted and the
manager validates the GGUF header before installing.

You can also use its CLI:

```sh
bash start-server.sh --list
bash start-server.sh --select Qwen3-8B-Q4_K_M.gguf
bash start-server.sh --download Qwen/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf
bash start-server.sh --start
```

The first downloaded model is selected automatically if no active model has
been chosen. On an existing setup containing just the original Qwen3 4B file,
that model is selected automatically as well.

The server listens on port 8080 by default. Keep Termux open while the model
is running; Android may stop background processes to save battery. Find the
phone's current Wi-Fi IP in Android Wi-Fi settings. The manager prints the
base URL, API key, and API-key file location at launch. Keep the key private.

Optional launch settings can be supplied in Termux before starting the manager:

```sh
export LLAMA_PORT=8080
export LLAMA_CTX_SIZE=2048
export LLAMA_THREADS=4
bash start-server.sh
```

Large models and longer contexts can exceed available physical RAM or become
very slow. The advertised/loaded model name remains `superbot-phone`; no Mac
configuration change is needed when selecting another model.

## Connect Super Bot

On the computer running the Super Bot session server, set these values in
the repository's ignored `.env` file:

```dotenv
PHONE_MODEL_BASE_URL=http://PHONE_IP:8080/v1
TEST_PHONE_API_KEY=your-phone-api-key
```

These settings are only needed when using the phone's local model. To use
Gemini instead, choose it from menu option 6 and enter a Google AI Studio API
key at the hidden prompt; a local model URL and key are not needed. OpenAI is
also available in that menu: enter an OpenAI API key at the hidden prompt and
a model ID that supports Chat Completions with JSON schema responses. For direct
server startup, set `PHONE_MODEL_PROVIDER=openai`, `OPENAI_API_KEY`, and
`OPENAI_MODEL_NAME` in the server environment or ignored `.env` file. Chat content
and included context are sent to OpenAI over HTTPS.

Replace `PHONE_IP` and the example key locally; never commit or share the
actual key. Then restart the Mac session server with `python -m Ai.server`.
Clients continue connecting to that session server; it forwards model calls
to the phone.

For a basic network check from the Mac, visit
`http://PHONE_IP:8080/health`. Do not expose port 8080 to the public internet:
unless you enable HTTPS below, the API uses HTTP without TLS and is intended
only for a trusted local network. The GGUF's model card and license still apply
to every downloaded model.

## Optional: encrypt the phone link with HTTPS

The Super Bot session server and its clients are always HTTPS, but the link
from the session server to the phone is plain HTTP by default. The session
server prints a warning at startup while that is the case. To encrypt it, first
check that the installed `llama-server` was built with HTTPS support:

```sh
llama-server --help | grep -i ssl-
```

If `--ssl-cert-file` and `--ssl-key-file` are not listed, keep using HTTP on a
trusted network (or put a TLS proxy in front of the model). Otherwise, in
Termux, create a certificate for the phone's IP address and start the manager
with it:

```sh
pkg install openssl-tool
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout ~/.config/superbot/tls-key.pem -out ~/.config/superbot/tls-cert.pem \
    -days 3650 -subj "/CN=superbot-phone" -addext "subjectAltName=IP:PHONE_IP"
chmod 600 ~/.config/superbot/tls-key.pem
export LLAMA_SSL_CERT=$HOME/.config/superbot/tls-cert.pem
export LLAMA_SSL_KEY=$HOME/.config/superbot/tls-key.pem
bash start-server.sh
```

The manager then serves `https://PHONE_IP:8080/v1`. Copy only the certificate
(`tls-cert.pem`, which is public; never the key) to the Mac, for example by
printing it with `cat` and pasting it into a file. Then point the session
server at it in the repository's `.env` file:

```dotenv
PHONE_MODEL_BASE_URL=https://PHONE_IP:8080/v1
PHONE_MODEL_CA_CERT=/path/to/tls-cert.pem
```

Create the certificate again if the phone's IP address changes, because the
address is part of the certificate.

## Run Super Bot on the phone (recommended)

Create a release signing key on the Mac once, from a private directory outside the checkout. Keep the private key outside the
checkout and off the phone.

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out release-private.pem
openssl pkey -in release-private.pem -pubout -out release-public.pem
```

Transfer `release-public.pem` through the same trusted channel as the launcher
and place it at `~/.config/superbot/release-public.pem` on the phone. An alternate
trusted public key path can be set with `SUPER_BOT_RELEASE_KEY`.

From the Mac checkout, publish the current source and its detached signature.

```sh
bash Ai/termux/serve-from-mac.sh /path/to/release-private.pem
```

Set `MAC_SERVER_URL=http://MAC_IP:8765` on the phone and select menu option 7.
Choose the local phone model (default), Gemini, or OpenAI. Cloud providers send
chat content to their respective APIs and require an API key. OpenAI also requires
a model ID. The Mac serves only the release archive and
signature. The phone verifies the signature before extracting any files.

The updater checks for a release when option 7 is selected. To update a running server, stop it with Ctrl+C and
select option 7 again. The candidate stays in `~/super_bot/.releases` and becomes
active only after its HTTPS health response identifies both its signed build
and its new process launch. If verification fails, the launcher stops the
candidate and starts the previous installation. The existing install directory
remains the working directory, so relative configuration paths, `.env`, model
files, and the configured server identity remain in place. Releases should
keep any durable data formats backward compatible. The current backend keeps
active session state in RAM.

Use `SUPER_BOT_OFFLINE=1 bash start-server.sh` to start the installed release
with local dependencies and skip the download. Set `SUPER_BOT_PORT` in the
launcher environment for a custom secure server port.

It installs `cryptography` from Termux's prebuilt `python-cryptography` package
so pip does not need to build it on the phone. Uvicorn is installed without
its optional `standard` extras because those include `watchfiles`, which
requires a native build not needed to run the server. For `pydantic-core`, it
first tries a prebuilt Android wheel from
`https://eutalix.github.io/android-pydantic-core/`. If no wheel matches the
installed Termux Python, it installs Termux Rust and the prebuilt `maturin`
wheel from the Termux User Repository, then builds the package on the phone.
This fallback can take a while and needs available memory.

When using the phone's local model, `llama-server` binds to `127.0.0.1`
(override with `LLAMA_HOST`), so the model is unreachable from the network even
without TLS. With any provider, the only open port is the Super Bot HTTPS +
E2EE port (default 8000). After the download completes, connect the client to
`https://PHONE_IP:8000` and confirm the fingerprint. The local model link is
loopback, so no `PHONE_MODEL_CA_CERT` is needed. The launcher explicitly sets
`SUPER_BOT_ALLOW_LAN=1` only for this encrypted server so other devices can
connect to it.
