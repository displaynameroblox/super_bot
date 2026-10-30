# Manage local models in Termux

The phone runs `llama.cpp` and exposes one OpenAI-compatible API on trusted
Wi-Fi. The model manager lets you download, select, and remove GGUF models on
the phone without editing scripts. Super Bot always calls the server under
the stable model name `superbot-phone`, regardless of which GGUF is selected.

## Install or update the phone manager

Connect the Mac and phone to the same trusted Wi-Fi network. From the
repository root on the Mac, run:

```sh
    bash Ai/termux/serve-from-mac.sh
```

In Termux, stop the currently running model server with Ctrl+C, then fetch the
updated launcher using the command printed by the Mac script:

```sh
pkg install curl
curl --fail --location http://MAC_IP:8765/termux/start-server.sh -o start-server.sh
bash start-server.sh
```

Replace `MAC_IP` with the Mac's Wi-Fi address. The manager installs missing
Termux packages (`llama-cpp`, `curl`, `wget`, and coreutils) and keeps existing
models and the API key in place. The model files live in `~/models`; manager
settings and the API key live in `~/.config/superbot`.

## Use the model manager

Running `bash start-server.sh` opens a menu:

1. List installed `.gguf` models and show the selected model.
2. Download Qwen3 4B Q4_K_M (~2.5 GB), Qwen3 8B Q4_K_M (~5.0 GB),
   Gemma 1.1 7B instruct Q4_K_M (~5.3 GB), Gemma 2 9B instruct Q4_K_M
   (~5.8 GB), another Hugging Face GGUF, or the experimental locally trained
   model shared on Hugging Face (option 6; **still testing**). The 7B/9B models
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

The experimental option downloads the model directly from the
[Hugging Face repository](https://huggingface.co/Displaynamethecat/gemma-2-2b-it-friendly-gguf)
into `~/models` (about 1.7 GB). The download resumes if interrupted and the
manager validates the GGUF header before installing. This model is still being
tested; local inference compatibility and quality are not yet confirmed.

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
key at the hidden prompt; a local model URL and key are not needed.

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

Menu option 6 downloads/updates the `Ai` folder from the Mac server into
`~/super_bot/Ai`, installs its Python requirements, and runs the Super Bot
server. It asks whether to use the phone's local model (default) or Google
Gemini. Gemini requires a Google AI Studio API key, entered at a hidden prompt;
the key is kept in memory only for that server run. Gemini sends conversation
messages to Google. The default Mac URL is
`http://192.168.8.179:8765`; set `MAC_SERVER_URL` before starting the manager
to use a different address. Keep the Mac server running while option 6
downloads the files, and use trusted Wi-Fi.
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
without TLS. With either provider, the only open port is the Super Bot HTTPS +
E2EE port (default 8000). After the download completes, connect the client to
`https://PHONE_IP:8000` and confirm the fingerprint. The local model link is
loopback, so no `PHONE_MODEL_CA_CERT` is needed.
