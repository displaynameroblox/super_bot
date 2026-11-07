# Server administration and recovery

## Install and start

Install Python 3.10+ and a versioned wheel from the official
[GitHub releases](https://github.com/displaynameroblox/super_bot/releases).
No developer file host is required. Choose the wheel for the release you intend
to install; do not download code from an untrusted mirror.

macOS/Linux:

```sh
python3 -m venv ~/.venvs/super-bot
~/.venvs/super-bot/bin/python -m pip install /path/to/super_bot-VERSION-py3-none-any.whl
# Add ~/.venvs/super-bot/bin to PATH, or use the commands by absolute path.
super-bot-manager init --config /path/to/your/.env
super-bot-manager start
super-bot-manager status
super-bot-manager admin
```

Windows PowerShell (no administrator privileges):

```powershell
py -m venv "$HOME\super-bot-env"
& "$HOME\super-bot-env\Scripts\python.exe" -m pip install .\super_bot-VERSION-py3-none-any.whl
& "$HOME\super-bot-env\Scripts\super-bot-manager.exe" init --config .\.env
& "$HOME\super-bot-env\Scripts\super-bot-manager.exe" start
& "$HOME\super-bot-env\Scripts\super-bot-manager.exe" admin
```

For a development checkout, `python -m pip install -e .` installs the same
entry points. The current working directory's `.env` is **not** automatically
imported into a managed instance: use `init --config` or edit the instance's own
`.env` reported by `super-bot-manager config`. Environment variables override
that file. The manager owns `SUPER_BOT_SERVER_DIR` and stores server state under
that instance's `data` directory. `SUPER_BOT_PORT` overrides `PORT` if supplied.
Provider secrets belong in `.env`, never command arguments or source URLs.

Startup is offline: it does not fetch releases, install dependencies or contact
an inference provider. Provider configuration is still validated. Model calls
report their own availability errors. Use `python -m Ai.preflight --diagnose`
with the intended environment to explicitly check the provider, or set
`SUPER_BOT_REQUIRE_MODEL_ON_START=1` for strict startup probing. Health means
that the TLS application is ready, not that a remote model is reachable.

`super-bot-server` remains a manual foreground entry point using its original
configuration conventions. Managed foreground mode is
`super-bot-manager start --foreground`; Ctrl+C requests graceful shutdown.
There is no automatic service installation and no endless restart policy.

### Existing installations

Stop the old launcher/server first. Copy, do not move, its `.env` and server
identity/TLS directory:

```sh
super-bot-manager init --config /old/install/.env --server-data /old/server/data
super-bot-manager start
```

Find the old data path from the old `SUPER_BOT_SERVER_DIR` setting; otherwise
it is the application's normal data directory plus `server`. `init` refuses
an instance that already has configuration or data. It does not delete the old
installation. Verify the displayed client fingerprints remain the same before
retiring the old launcher. Client databases, profiles, knowledge and training
files stay in their existing locations and are never migrated by the updater.
A legacy `.releases/active` server tree is not silently adopted as trusted code;
install the bootstrap wheel and use the new signed manifest format.

### Termux

Copy the Super Bot wheel and `Ai/termux/install-wheel.sh` to Termux. For USB
testing, transfer both with `adb push` to the phone's Downloads directory and
grant Termux storage access with `termux-setup-storage`. Then run:

```sh
cd ~/storage/downloads
bash install-wheel.sh super_bot-1.0.0-py3-none-any.whl
```

The installer uses Termux's prebuilt cryptography package and accepts only
wheels for pip dependencies. If pip cannot find a compatible Android wheel
(for example for pydantic-core or an Argon2 binding), pass a trusted directory
of prebuilt wheels as the second argument. They must match the phone's
architecture and Python version. The installer exits rather than compiling
Rust on the phone. Android wheel availability still needs device validation.

Hugging Face downloads and PDF indexing are optional. Install
`super-bot[models]` or `super-bot[pdf]` where you use those features. For a
development checkout, use `python -m pip install -e '.[models,pdf]'`.

The existing model menu remains available. When the shared Python manager is
installed, its secure-server action delegates to that manager. Start the local
model separately with option 5 when needed; server transport can start without
it. The old archive updater remains a migration fallback. It has no default LAN
host. It requires an explicitly configured URL and trusted key.

Use `super-bot-manager admin --plain` in small/dumb terminals. Android may kill
background processes; foreground mode and, where available, Termux wake locks
can help, but the manager cannot override Android's power management. No Android
service or boot persistence is claimed.

## Command reference

Global options precede the command:

```sh
super-bot-manager --instance /path/to/instance --timeout 45 --json status
```

The default instance is `instances/default` under `SUPER_BOT_DATA_DIR`, or the
OS-specific Super Bot application-data directory (macOS Application Support,
Windows APPDATA, Linux/Termux XDG_DATA_HOME). Each instance needs distinct TCP
`PORT` and UDP `DISCOVERY_PORT` values. Use a local filesystem, not NFS/SMB, for
process locks and transactional state.

| Command | Behavior |
|---|---|
| `init [--config FILE] [--server-data DIR]` | Copy configuration and identity into a new instance |
| `start [--foreground]` | Start installed code offline; require verified TLS readiness |
| `stop` | Request bounded graceful shutdown of this managed instance |
| `restart` | Stop, then start once |
| `status` | Health, build, local client/session counts, log path, recovery state |
| `config` | Safe path/source overview; never dumps `.env` |
| `logs [--lines 40] [--follow]` | Bounded log tail or follow stream |
| `source list` | Available source types and selected source/channel |
| `source use official [--channel stable\|development]` | Select pinned official GitHub releases |
| `source use local DIR --key PUBLIC.pem --trust-key` | Explicitly trust a developer release key |
| `source use lan URL --key PUBLIC.pem --trust-key [--allow-http]` | Select an explicitly trusted LAN source |
| `update check [--offline]` | Verify manifest and cache archive; no running-server changes |
| `update apply [--offline]` | Stage, back up, test, activate or restore previous release |
| `update rollback` | Activate the previous verified version, including bootstrap fallback |
| `admin [--plain] [--once]` | Keyboard dashboard, line menu, or noninteractive snapshot |
| `remote HOST ACTION` | Allowlisted administration over authenticated SSH |

Results are JSON (also with `--json`); errors contain `error.code` and
`error.message`. Exit codes: 0 success, 1 operation/verification failure,
2 invalid arguments, 3 concurrent operation/already running, 4 failed readiness
or graceful shutdown, 130 interruption. `status` reports stopped/unhealthy as
state rather than treating inspection itself as a failed operation. Parse state
for monitoring. `logs --follow` streams log text rather than one JSON document.
The existing model download/serve/train/index and client-backup commands retain
their original CLI behavior.

The dashboard supports Up/Down or j/k, Enter, `l` for live log view, `:` for a
manager command (including source selection and `remote HOST status`), and q/Esc.
Long actions run in a worker; wait for their bounded completion before quitting.
Small screens show the selected action; `--plain` supports line input and screen
readers. Non-TTY input automatically prints one snapshot. The existing chat TUI
is independent.

## Release trust and sources

Official source resolution uses GitHub's release API and versioned release asset
URLs, never raw branches. A release consists of `manifest.json`, `manifest.sig`,
and `super-bot-release.tar.gz`. The manifest binds version, channel, Python and
OS/architecture compatibility, dependency constraints, data-schema contract,
archive size and SHA-256. RSA PKCS#1 v1.5/SHA-256 signatures use the same
cryptographic scheme as the legacy updater; the new manager uses Python
cryptography rather than an external OpenSSL command.

**Maintainer prerequisite:** this checkout does not contain an official release
public key. Official update commands fail closed until a maintainer provisions
`Ai/release-public.pem` and the corresponding GitHub Actions secret
`RELEASE_SIGNING_KEY`. There is no insecure bypass. A bootstrap wheel still runs
normally offline. Do not mistake implementation of the publisher for an already
published signed release.

For developer builds, create/manage an RSA signing key outside the repository,
export its public key, independently compare its SHA-256 fingerprint, then:

```sh
python tools/build_release.py --key /secure/private.pem --version 1.1.0.dev1 \
  --channel development --output ./builds
super-bot-manager source use local ./builds --channel development \
  --key /trusted/public.pem --trust-key
super-bot-manager update check
super-bot-manager update apply --offline
```

`source use` reports the SHA-256 fingerprint of the exact public PEM bytes. For
LAN HTTP, explicit `--allow-http` acknowledges that transport is plaintext;
signatures remain mandatory. HTTPS uses normal certificate verification and
rejects downgrade redirects. SSH is preferred for remote administration.
Never fetch a new trust key from the same untrusted source you are verifying.
Source changes do not inherit another source's key. Cached updates are
reverified against the selected source's trust anchor and channel.

Downloads have size limits, per-socket timeouts and a total read deadline; failed
or interrupted downloads never replace the cache. Archives permit only regular
files within `Ai/`, reject traversal, links, duplicate/case-colliding paths,
Windows reserved names, alternate streams and oversized expansion. Stable
versions cannot silently change content. Lower versions require explicit rollback.

Dependencies come from the same requirements file as the bootstrap package.
The updater checks installed dependency versions before activation and never
installs dependencies into a running environment. A release needing different
dependencies is refused with an instruction to upgrade the bootstrap package.
For a bootstrap upgrade, stop managed instances, preserve the environment for
manual fallback, and install a new versioned wheel into a new virtual environment.
Point that manager at the existing instance using `--instance`. Automatic
bootstrap/dependency-environment rollback is not implemented.

## Architecture and recovery

`local_manager` dispatches server commands to `manager_cli`; existing model and
library commands are preserved. `manager_runtime` owns atomic state writes,
OS-specific kernel locks and process control. `manager_worker` holds the server
lock while loading the selected server code. `releases` acquires and verifies
artifacts and coordinates activation; `admin` is a separate view over the CLI
operations. The installed bootstrap runs the updater, never the candidate.

An operation lock serializes mutations; a separate lifetime lock prevents
multiple server children. OS locks release on process death without stale PID
file removal. Stop requests are private instance files bound to a random launch
ID, not kill commands using possibly recycled PIDs. Readiness checks pin the
actual configured TLS certificate and match both build and launch ID. Aggregate
session/client counts are local files, not public administrative endpoints.
The application's authentication, identity handshake, encrypted gateway and
LAN discovery remain unchanged.

Updates verify and extract before stopping the server, then back up the owned
server data and `.env`, write a durable transaction journal, launch the candidate,
and verify readiness. Success commits an atomic active pointer. An unhealthy
candidate restores the backup and prior release, with at most one fallback start.
If the server was stopped before applying, the candidate is still tested and
then stopped. Applying an update disconnects temporary sessions; client-owned
saved histories remain intact. Backups copy stopped server files, including any
SQLite journals; no live database migration is attempted.

Only schema 1 with an empty migration list is accepted. Releases declaring
migrations or incompatible schema changes are refused **before stopping**. This
is deliberately a no-migration update contract, not a claim that arbitrary
irreversible migrations can be rolled back. External TLS files referenced in
`.env` remain external; back them up separately. No client data is included in
server backups.

If an update is interrupted, `transaction.json` remains. Next `start`, `apply`, or
`rollback` stops the interrupted candidate, restores the recorded backup and
previous active pointer, and restores the prior running state. Status flags
pending recovery. Failed candidate data is retained as `failed-data-*` for
inspection. If recovery itself fails, the journal remains for another attempt;
fix the permission/disk/configuration problem and retry. Do not delete it blindly.

```sh
super-bot-manager status
super-bot-manager logs --lines 80
super-bot-manager start                 # performs pending recovery
super-bot-manager update rollback       # previous verified release
```

Backups, cached releases and failed-state directories are intentionally retained;
there is no automatic pruning. Keep enough disk space for an archive, extracted
candidate and a full server-state backup. Logs are local and may contain sensitive
application diagnostics: share only reviewed excerpts. POSIX instance directories
are created private; Windows uses the user's inherited application-data ACLs.
Do not put an instance in a shared writable folder.

## Remote administration

Use conventional SSH with an independently verified host key in `known_hosts`
and key/agent authentication. Set up the account and SSH host alias through your
OS's normal SSH tooling; install the manager in that account's PATH:

```sh
super-bot-manager remote my-server status
super-bot-manager remote my-server restart
super-bot-manager remote my-server check
# Interactive dashboard, or custom instance paths:
ssh -t my-server 'super-bot-manager --instance /srv/my-private-instance admin'
```

The wrapper enforces `StrictHostKeyChecking=yes`, batch authentication and a
connection timeout. It accepts a fixed action list, validates the host token and
passes no user-provided remote shell command. The target account's OS permissions
provide authorization. Configure ports/keys in SSH config, not shell fragments.
Windows SSH servers may require PATH and shell setup; this has not been tested
on real Windows hosts. No management API, listener or unauthenticated admin route
is introduced. LAN exposure of the chat server still requires explicit `HOST`
and `SUPER_BOT_ALLOW_LAN=1`; TLS and client fingerprint trust remain mandatory.

## Publishing

Maintain an RSA key of at least 2048 bits outside the checkout. Commit only the
public PEM at `Ai/release-public.pem`; put its private counterpart in the GitHub
secret. The workflow checks they match, checks tag/package versions, builds the
bootstrap wheel/sdist and signed server assets, and publishes only on a version
tag after the OS test matrix passes. The source archive deliberately allowlists
Python modules and web assets; it excludes tests, `.env`, databases and keys.
The bootstrap alone includes the explicitly pinned public key. Do not reuse
application TLS/identity keys for release signing. Signing-key rotation requires
a new trusted bootstrap/key distribution; automatic rotation is not implemented.

In the admin dashboard or `admin --plain`, choose **Download Qwen3 4B**,
**Download Qwen3 8B**, a Gemma preset (2B, 4B, or 9B), or
**Paste Hugging Face GGUF link**. Install the
`models` extra first. Paste an HTTPS Hugging Face link to the GGUF file
(the file page or its download link). Downloads go into the Super Bot data
directory under `models`, grouped by repository. The result shows the file
path. Choose **Pick installed model**, select the file, then choose **Start**
(or **Restart** if already running). With `llama-server` on PATH, the managed
server starts a loopback model runner and waits for it to load. Stop shuts
down both processes. Use **Use configured provider** in the picker to return
to the instance's existing provider settings on the next start. The selection
is per instance and requires no changes to `.env`. Model startup diagnostics
are in `model.log` beside the instance's server log.

On macOS, if a selected GGUF needs `llama-server`, Start and Restart offer
to install llama.cpp with your existing Homebrew installation. The dashboard
also offers to install Hugging Face support when you request a model download
and that Python dependency is missing. Both prompts default to cancellation.
Approved installations are logged in the instance's `setup.log`.
